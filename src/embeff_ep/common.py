# Copyright (C) 2022 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

"""Core ExecutionPlatform client, shared by every test-framework frontend.

Contains the WebSocket transport (``Remote_WS``), argument/result marshalling,
ep-config introspection (``ep_config``) and the session lifecycle (``EPSession``).
User-facing messages go through ``reporter``, which a frontend may replace, and
failures are raised as plain exceptions.
"""

import asyncio
import base64
import importlib.metadata
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import UserString
from collections.abc import Iterable, Mapping
from enum import Enum
from functools import wraps
from hashlib import sha256
from io import IOBase
from threading import Thread
from time import sleep
from unicodedata import normalize
from urllib.error import HTTPError
from zipfile import ZipFile

from . import __version__


logger = logging.getLogger("ep")


class Reporter:
    """Neutral sink for user-facing messages.

    Defaults route to stdout/stderr. A frontend can replace the module-level
    ``reporter`` instance (see robot.py) so its own logging is used instead.
    """

    def error(self, msg):
        """Report an error to the user. Does not raise or abort by itself."""
        print(msg, file=sys.stderr)

    def console(self, msg):
        """Write an informational message to the console."""
        print(msg)


reporter = Reporter()


try:
    from fastapi_websocket_rpc import RpcMethodsBase, WebSocketRpcClient
    from fastapi_websocket_rpc.simplewebsocket import SimpleWebSocket
    from fastapi_websocket_rpc.websocket_rpc_client import isNotForbbiden
    from websockets.exceptions import InvalidStatus
except ImportError:
    reporter.error(
        "fastapi_websocket_rpc module not available. Please install it.\nGo to https://ep-docs.embeff.com/#/GettingStarted?id=requirements for instructions."
    )
    exit(1)

EP_CLIENT_VERSION = __version__
SEMVER_REGEX = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\..+$")
URL_REGEX = re.compile(r"(?P<user>\S+):(?P<password>\S+)@(?P<address>\S+)")


# ---------------------------------------------------------------------
# Type helpers (replace robot.utils)
# ---------------------------------------------------------------------
def is_string(item):
    """Return True if ``item`` is a string."""
    return isinstance(item, str)


def is_bytes(item):
    """Return True if ``item`` is a bytes-like object."""
    return isinstance(item, (bytes, bytearray))


def is_number(item):
    """Return True if ``item`` is an int or a float."""
    return isinstance(item, (int, float))


def is_dict_like(item):
    """Return True if ``item`` behaves like a mapping."""
    return isinstance(item, Mapping)


def is_list_like(item):
    """Return True if ``item`` is iterable but not a string, bytes or a stream."""
    if isinstance(item, (str, bytes, bytearray, UserString, IOBase)):
        return False
    return isinstance(item, Iterable)


def safe_str(item):
    """Convert ``item`` to a normalized string. Never raises."""
    return normalize("NFC", _safe_str(item))


def _safe_str(item):
    if isinstance(item, str):
        return item
    if isinstance(item, (bytes, bytearray)):
        return item.decode("latin-1")
    try:
        return str(item)
    except Exception as e:
        return f"<Unrepresentable object {type(item).__name__}. Error: {e}>"


class DotDict(dict):
    """dict with attribute access, for returned dictionary values.
    Mirrors robot.utils.DotDict.
    """

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None

    def __setattr__(self, key, value):
        self[key] = value

    def __delattr__(self, key):
        try:
            del self[key]
        except KeyError:
            raise AttributeError(key) from None


class EPRemoteError(Exception):
    """Raised by the transport when a remote keyword fails.
    Maps to ``robot.errors.RemoteError``.
    """

    def __init__(self, message, traceback="", fatal=False, continuable=False):
        super().__init__(message)
        self.message = message
        self.traceback = traceback
        self.fatal = fatal
        self.continuable = continuable


# ---------------------------------------------------------------------
# Common WebSockets serialization
# (mirrors services/server/remote_server/ws_common.py)
# ---------------------------------------------------------------------
class Binary:
    """Wrapper for binary data."""

    def __init__(self, data=None):
        if data is None:
            data = b""
        else:
            if not isinstance(data, (bytes, bytearray)):
                raise TypeError("expected bytes or bytearray, not %s" % data.__class__.__name__)
            data = bytes(data)  # Make a copy of the bytes!
        self.data = data

    ##
    # Get buffer contents.
    #
    # @return Buffer contents, as an 8-bit string.

    def __str__(self):
        return str(self.data, "latin-1")  # XXX encoding?!

    def __eq__(self, other):
        if isinstance(other, Binary):
            other = other.data
        return self.data == other

    @staticmethod
    def fromJSON(data):
        """Decode a ``Binary`` object back into bytes."""
        if data["__type__"] == "Binary":
            return base64.b64decode(data["value"])
        return data

    def toJSON(self):
        """Encode the wrapped bytes as a base64 object."""
        encoded = base64.b64encode(self.data).decode("ascii")
        return {"__type__": "Binary", "value": encoded}


def remote_types_object_hook(dct):
    """JSON object hook that revives EP-encoded types (currently ``Binary``)."""
    if "__type__" in dct:
        if dct["__type__"] == "Binary":
            return Binary.fromJSON(dct)
    return dct


class RemoteJsonEncoder(json.JSONEncoder):
    """JSON encoder that serializes bytes as EP ``Binary`` objects."""

    def default(self, z):
        """Encode values the standard encoder cannot handle."""
        if isinstance(z, Binary):
            return z.toJSON()
        if isinstance(z, (bytes, bytearray)):
            return Binary(z).toJSON()
        return super().default(z)


class SerializingWebSocket(SimpleWebSocket):
    """WebSocket wrapper that applies the EP JSON encoding to every message."""

    def __init__(self, websocket: SimpleWebSocket):
        self._websocket = websocket
        self.encoder = RemoteJsonEncoder()

    def _serialize(self, msg):
        if hasattr(msg, "model_dump"):
            # pydantic v2: v1's .json(encoder=...) was removed, so re-implement it
            # with model_dump(mode="python") (keeps raw bytes/Binary objects as-is) +
            # json.dumps(default=...) for the same custom Binary/bytes encoding.
            return json.dumps(msg.model_dump(mode="python"), default=self.encoder.default)
        return msg.json(encoder=self.encoder.default)

    def _deserialize(self, buffer):
        return json.loads(buffer, object_hook=remote_types_object_hook)

    async def connect(self, uri: str, **connect_kwargs):
        """Open the underlying connection to ``uri``."""
        await self._websocket.connect(uri, **connect_kwargs)

    async def send(self, msg):
        """Serialize and send ``msg``."""
        await self._websocket.send(self._serialize(msg))

    async def recv(self):
        """Receive one message and deserialize it, or ``None`` if the connection closed."""
        msg = await self._websocket.recv()
        if msg is None:
            return None

        return self._deserialize(msg)

    async def close(self, code: int = 1000):
        """Close the underlying socket with the given status ``code``."""
        await self._websocket.close(code)


# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# Remote WS
# ---------------------------------------------------------------------
class DeviceBusy(Exception):
    """The EP is currently in use by someone else; the caller may retry."""


class Remote_WS(object):
    """WebSocket RPC client for the EP's remote keyword server.

    Authenticates against the EP (or a central-management node, which may
    redirect to an assigned EP), then exposes the remote library over
    ``run_keyword`` and the ``get_keyword_*`` introspection calls.
    """

    def __init__(self, unresolved_addr, ep_user, ep_password, active_dut, tags, timeout=None):
        """Connect to ``unresolved_addr``, waiting up to ``timeout`` seconds for a free EP."""
        self._lib_info = None

        self._lib_loop = asyncio.new_event_loop()
        self._loop_thread = Thread(name="EP Communication", target=self._run_background_loop, daemon=True)
        self._loop_thread.start()
        self._client = None
        self._unresolved_address = unresolved_addr
        self._ep_address = None

        def best_known_addr():
            return self._ep_address or self._unresolved_address

        doc_hint = "Please refer to https://ep-docs.embeff.com/#/EP_Config?id=the-robot-library for details."

        try:
            access_token = None
            token_url = f"http://{self._unresolved_address}/token"
            request_dict = {"username": ep_user, "password": ep_password, "dut": active_dut, "tags": tags}
            request_data = urllib.parse.urlencode(request_dict, doseq=True).encode("ascii")

            deadline = time.monotonic() + timeout if timeout is not None else None
            while True:
                try:
                    token_req = urllib.request.Request(token_url, request_data)
                    with urllib.request.urlopen(token_req) as response:
                        response_data = json.loads(response.read())
                        access_token = response_data["access_token"]
                        self._ep_address = response_data.get(
                            "ep_address", self._unresolved_address
                        )  # default to originally specified address
                        # If the original ep_address pointed to a management node, the response contains the address of the assigned EP.
                        # If the original ep_address pointed to an ExecutionPlatform, the response does not contain any address.
                    if self.was_redirected:
                        reporter.console(f"Redirected by management to an EP at {self._ep_address}.")
                    break
                except HTTPError as e:
                    body = e.read()
                    try:
                        detail = json.loads(body).get("detail", "") if body else ""
                    except json.JSONDecodeError:
                        detail = ""
                    if e.code == 503:  # Service Unavailable: all EPs matching the criteria are busy
                        if deadline is not None and time.monotonic() >= deadline:
                            self.close()
                            reporter.error(f"Timed out after {timeout}s waiting for an available EP: {detail}")
                            exit(1)
                        reporter.console(f"{detail} Waiting...")
                        sleep(2)
                        continue
                    if e.code == 421:  # Misdirected Request: EP is under central management
                        reporter.error(f"{detail} {doc_hint}")
                    elif e.code == 409:  # Conflict: no EP matches the requested dut/tags, or no access
                        reporter.error(f"{detail} {doc_hint}")
                    elif e.code == 401:  # Unauthorized: invalid username or password
                        reporter.error(f"Can't connect to '{best_known_addr()}': {detail} {doc_hint}")
                    elif e.code == 403:
                        reporter.error(f"Can't connect to '{best_known_addr()}': {detail} {doc_hint}")
                    else:  # 404 Not Found or unexpected
                        reporter.error(
                            f"Can't connect to '{best_known_addr()}': Code {e.code}. Check your settings for ${{ep_url}}. {doc_hint}"
                        )
                    self.close()
                    exit(1)
                except KeyboardInterrupt:
                    exit(1)

            self._client = self.createClient(f"ws://{self._ep_address}:8270/robot", token=access_token)
        except InvalidStatus as e:
            if isNotForbbiden(e):
                reporter.error(f"Can't connect to {best_known_addr()}: Code {e.response.status_code}.")
            else:
                reason = e.response.headers.get("detail", "Invalid username or password")
                reporter.error(f"Can't connect to {best_known_addr()}: {reason} {doc_hint}")
            self.close()
            exit(1)
        except Exception:
            reporter.error(f"Can't connect to {best_known_addr()}. Is the EP powered and reachable in your network?")
            self.close()
            exit(1)

    def get_ep_address(self):
        """Return the address of the EP in use (may differ after a redirect)."""
        return self._ep_address

    @property
    def was_redirected(self):
        """Return True if central management assigned a different EP than requested."""
        return self._ep_address != self._unresolved_address  # after __init__, these only differ when CM redirected

    def _run_background_loop(self) -> None:
        asyncio.set_event_loop(self._lib_loop)
        self._lib_loop.run_forever()

    def run_in_lib_loop(f):
        """Decorator: run a coroutine on the background loop and block for its result."""

        @wraps(f)
        def wrapper(self, *args, **kwargs):
            runner = f(self, *args, **kwargs)
            result = asyncio.run_coroutine_threadsafe(runner, self._lib_loop).result()
            return result

        return wrapper

    @run_in_lib_loop
    async def createClient(self, uri, token) -> WebSocketRpcClient:
        """Open the RPC connection to ``uri`` using the given bearer token."""
        client = WebSocketRpcClient(
            uri,
            RpcMethodsBase(),
            serializing_socket_cls=SerializingWebSocket,
            additional_headers={"Authorization": f"Bearer {token}"},
        )
        result = await client.__connect__()
        return result

    @run_in_lib_loop
    async def exitClient(self):
        """Close the RPC connection."""
        await self._client.__aexit__()

    @run_in_lib_loop
    async def get_keyword_names(self):
        """Return the names of all keywords the EP currently exposes."""
        self._lib_info = await self.get_library_information()
        return [name for name in self._lib_info if not (name[:2] == "__" and name[-2:] == "__")]

    @run_in_lib_loop
    async def get_keyword_arguments(self, name):
        """Return the argument spec of keyword ``name``."""
        return await self._get_kw_info(name, "args")

    @run_in_lib_loop
    async def get_keyword_types(self, name):
        """Return the argument types of keyword ``name``."""
        return await self._get_kw_info(name, "types", default=())

    @run_in_lib_loop
    async def get_keyword_tags(self, name):
        """Return the tags of keyword ``name``."""
        return await self._get_kw_info(name, "tags")

    @run_in_lib_loop
    async def get_keyword_documentation(self, name):
        """Return the documentation of keyword ``name``."""
        return await self._get_kw_info(name, "doc")

    async def get_library_information(self):
        """Fetch the full keyword catalogue (names, args, types, tags, docs) from the EP."""
        response = await self._client.other.get_library_information()
        return response.result

    async def _get_kw_info(self, kw, info, default=None):
        if self._lib_info is None:
            self._lib_info = await self.get_library_information()
        return self._lib_info[kw].get(info, default)

    def close(self):
        """Close the connection and stop the background event loop."""
        if self._client is not None:
            try:
                self.exitClient()
                self._client = None
            except Exception as e:
                reporter.console(
                    f"Exception occurred while deconstructing\
                               Remote Client: {e}"
                )
        if self._lib_loop:
            if self._lib_loop.is_running():
                self._lib_loop.call_soon_threadsafe(self._lib_loop.stop)
            self._loop_thread.join()

    @run_in_lib_loop
    async def run_keyword(self, name, args, kwargs):
        """Run keyword ``name`` on the EP and return its value.

        Raises ``DeviceBusy`` if the EP is in use, or ``EPRemoteError`` if the
        keyword itself failed.
        """
        coercer = ArgumentCoercer()
        args = coercer.coerce(args)
        kwargs = coercer.coerce(kwargs)
        rpcResult = await self._client.other.run_keyword(name=name, args=args, kwargs=kwargs)
        result = RemoteResult(rpcResult.result)
        if result.status == "BUSY":
            raise DeviceBusy(result.error)
        elif result.status != "PASS":
            raise EPRemoteError(result.error, result.traceback, result.fatal, result.continuable)
        return result.return_

    def __del__(self):
        self.close()


class ArgumentCoercer:
    """Converts keyword arguments into JSON-encodable types.

    Strings containing non-printable bytes and any bytes-like value become
    ``Binary``; lists and dicts are coerced recursively; anything else is
    stringified.
    """

    binary = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
    non_ascii = re.compile("[\x80-\xff]")

    def coerce(self, argument):
        """Return ``argument`` converted to a JSON-encodable representation."""
        for handles, handler in [
            (is_string, self._handle_string),
            (is_bytes, self._handle_bytes),
            (is_number, self._pass_through),
            (is_dict_like, self._coerce_dict),
            (is_list_like, self._coerce_list),
            (lambda arg: True, self._to_string),
        ]:
            if handles(argument):
                return handler(argument)

    def _handle_string(self, arg):
        if self._string_contains_binary(arg):
            return self._handle_binary_in_string(arg)
        return arg

    def _string_contains_binary(self, arg):
        return self.binary.search(arg) or (is_bytes(arg) and self.non_ascii.search(arg))

    def _handle_binary_in_string(self, arg):
        try:
            if not is_bytes(arg):
                # Map Unicode code points to bytes directly
                arg = arg.encode("latin-1")
        except UnicodeError:
            raise ValueError("Cannot represent %r as binary." % arg) from None
        return Binary(arg)

    def _handle_bytes(self, arg):
        return Binary(arg)

    def _pass_through(self, arg):
        return arg

    def _coerce_list(self, arg):
        return [self.coerce(item) for item in arg]

    def _coerce_dict(self, arg):
        return {self._to_key(key): self.coerce(arg[key]) for key in arg}

    def _to_key(self, item):
        item = self._to_string(item)
        self._validate_key(item)
        return item

    def _to_string(self, item):
        item = safe_str(item) if item is not None else ""
        return self._handle_string(item)

    def _validate_key(self, key):
        if isinstance(key, Binary):
            raise ValueError("Dictionary keys cannot be binary. Got %r." % (key.data,))


class RemoteResult:
    """Parsed result dictionary returned by the EP for one keyword call.

    Exposes ``status``, ``output``, ``return_``, ``error``, ``traceback`` and
    the ``fatal`` / ``continuable`` failure flags. Returned dictionaries are
    wrapped in ``DotDict`` so callers can use attribute access.
    """

    def __init__(self, result):
        """Parse the raw result dictionary received from the EP."""
        if not (is_dict_like(result) and "status" in result):
            raise RuntimeError("Invalid remote result dictionary: %s" % result)
        self.status = result["status"]
        self.output = safe_str(self._get(result, "output"))
        self.return_ = self._get(result, "return")
        self.error = safe_str(self._get(result, "error"))
        self.traceback = safe_str(self._get(result, "traceback"))
        self.fatal = bool(self._get(result, "fatal", False))
        self.continuable = bool(self._get(result, "continuable", False))

    def _get(self, result, key, default=""):
        value = result.get(key, default)
        return self._convert(value)

    def _convert(self, value):
        if isinstance(value, Binary):
            return bytes(value.data)
        if is_dict_like(value):
            return DotDict((k, self._convert(v)) for k, v in value.items())
        if is_list_like(value):
            return [self._convert(v) for v in value]
        return value


# ---------------------------------------------------------------------


def keyword_name(endpoint_name, function_name):
    """Return the keyword name the EP server expects for ``endpoint_name.function_name``."""
    return f"{endpoint_name} {function_name}"


class ep_config:
    """An ``.ep-config.zip`` file and the keyword catalogue it declares.

    Reads the DUT SKU and introspects the configuration's generated test module
    so a frontend can offer code completion before the EP connection exists.
    """

    def __init__(self, config_path):
        """Load the ep-config at ``config_path`` and index its keywords."""
        self.config_path = config_path
        self.config_name = os.path.basename(config_path).split(".")[
            0
        ]  # Possible because "." is forbidden in config names

        if not os.path.isfile(self.config_path):
            err = f"Configuration file '{self.config_path}' does not exist."
            raise ValueError(err)
        self.config_keywords_info = self.get_library_information()
        self.dut_sku = self.get_dut_sku()

    def get_dut_sku(self):
        """Return the DUT SKU this configuration was built for."""
        try:
            with ZipFile(self.config_path, "r") as zf:
                with zf.open("dut-sku", "r") as f:
                    return f.readline().strip()
        except Exception:
            raise ValueError(
                f"Unexpected internal structure in {self.config_path}. Did you alter the file? This is currently forbidden. Please refer to https://ep-docs.embeff.com/#/EP_Config?id=configurations or contact support."
            ) from None

    def get_library_information(self):
        """Introspect the configuration and return its keyword catalogue.

        Endpoint methods are exposed as ``"<Endpoint> <method>"``.
        """
        initial_sys_path = sys.path
        try:
            sys.path = [self.config_path]
            config_keywords_info = {}

            try:
                config = importlib.import_module(self.config_name + "_test")
            except ModuleNotFoundError:
                try:
                    # Old import (before 9.5.0)
                    importlib.import_module(self.config_name)
                    # If the line above passes, this means we have a config file with an old internal structure
                    raise ValueError(
                        f"The configuration was created for a version prior to 9.5.0. Update with an ep-config that matches this embeff-ep version ({EP_CLIENT_VERSION}): {self.config_path}"
                    )
                except ModuleNotFoundError:
                    raise ValueError(
                        f"Unexpected internal structure in {self.config_path}. Did you rename the file? This is currently forbidden. Please refer to https://ep-docs.embeff.com/#/EP_Config?id=configurations"
                    ) from None

            Configured_endpoints = {
                name: value
                for name, value in vars(config.Configuration).items()
                if not (name[:2] == "__" and name[-2:] == "__")
            }

            for endpoint_name, endpoint_type in Configured_endpoints.items():
                functions = inspect.getmembers(
                    endpoint_type, predicate=lambda o: inspect.ismethod(o) or inspect.isfunction(o)
                )
                for function_name, function_object in functions:
                    args, varargs, kwargs, defaults, *_ = inspect.getfullargspec(function_object)
                    if inspect.ismethod(function_object):
                        args = args[1:]  # drop 'self'
                    if defaults:
                        args, names = args[: -len(defaults)], args[-len(defaults) :]
                        args += ["%s=%s" % (n, d) for n, d in zip(names, defaults, strict=True)]
                    if varargs:
                        args.append("*%s" % varargs)
                    if kwargs:
                        args.append("**%s" % kwargs)

                    tags = getattr(function_object, "robot_tags", [])
                    docs = inspect.getdoc(function_object) or ""

                    endpoint_properties = {"args": args, "tags": tags, "doc": docs, "types": []}
                    config_keywords_info[keyword_name(endpoint_name, function_name)] = endpoint_properties
            return config_keywords_info

        finally:
            sys.path = initial_sys_path

    def get_keyword_names(self):
        """Return the names of all keywords this configuration provides."""
        if self.config_keywords_info is None:
            self.config_keywords_info = self.get_library_information()
        return [name for name in self.config_keywords_info if not (name[:2] == "__" and name[-2:] == "__")]

    def get_keyword_arguments(self, name):
        """Return the argument spec of keyword ``name``."""
        return self._get_kw_info(name, "args")

    def get_keyword_types(self, name):
        """Return the argument types of keyword ``name``."""
        return self._get_kw_info(name, "types", default=())

    def get_keyword_tags(self, name):
        """Return the tags of keyword ``name``."""
        return self._get_kw_info(name, "tags")

    def get_keyword_documentation(self, name):
        """Return the documentation of keyword ``name``."""
        return self._get_kw_info(name, "doc")

    def _get_kw_info(self, kw, info, default=None):
        if self.config_keywords_info is None:
            self.config_keywords_info = self.get_library_information()
        return self.config_keywords_info[kw].get(info, default)

    def get_content(self):
        """Return the raw bytes of the ep-config file, for upload to the EP."""
        with open(self.config_path, "rb") as f:
            return f.read()

    def build_live_configuration(self, dispatch):
        """Return a live ``Configuration`` whose endpoint methods call the EP.

        The ep-config ships a ``Configuration`` class. This instantiates it and,
        for every endpoint method, installs a wrapper that calls
        ``dispatch(keyword, args, kwargs)``, so a non-Robot frontend (e.g.
        the pytest plugin) can offer the exact keywords Robot exposes. The generated
        classes supply the endpoint and method names only; the wrappers take
        ``*args, **kwargs`` and carry no signature or docstring.
        """
        initial_sys_path = sys.path
        try:
            sys.path = [self.config_path]
            module = importlib.import_module(self.config_name + "_test")
        finally:
            sys.path = initial_sys_path

        configuration = module.Configuration()
        endpoints = {
            name: value
            for name, value in vars(module.Configuration).items()
            if not (name[:2] == "__" and name[-2:] == "__")
        }
        for endpoint_name, endpoint_instance in endpoints.items():
            endpoint_type = type(endpoint_instance)
            live_endpoint = endpoint_type()
            functions = inspect.getmembers(
                endpoint_type, predicate=lambda o: inspect.isfunction(o) or inspect.ismethod(o)
            )
            for function_name, _ in functions:
                if function_name[:2] == "__" and function_name[-2:] == "__":
                    continue
                keyword = keyword_name(endpoint_name, function_name)
                setattr(live_endpoint, function_name, self._make_keyword_dispatcher(dispatch, keyword))
            setattr(configuration, endpoint_name, live_endpoint)
        return configuration

    @staticmethod
    def _make_keyword_dispatcher(dispatch, keyword):
        def call(*args, **kwargs):
            return dispatch(keyword, list(args), kwargs)

        return call


class VSCode_helper:
    """Drives VS Code for on-target debugging (launch config + remote control)."""

    @staticmethod
    def update_launch(file, ep_addr):
        """Point the "Debug on ExecutionPlatform" launch config at ``file`` on ``ep_addr``."""
        from socket import getaddrinfo, AF_INET

        addr_info = getaddrinfo(ep_addr, None)
        ep_ip = None
        for a in addr_info:
            if a[0] == AF_INET:
                ep_ip = a[-1][0]
        if ep_ip is None:
            raise Exception(f"Address {ep_addr} couldn't be resolved.")

        launch_file_path = os.path.join(".vscode", "launch.json")
        with open(launch_file_path, "r+") as launch_file:
            try:
                content = json.load(launch_file)
                for config in content["configurations"]:
                    if config["name"] == "Debug on ExecutionPlatform":
                        config["executable"] = os.path.splitext(file)[0] + ".elf"
                        config["ipAddress"] = ep_ip
                        break
                launch_file.seek(0)
                json.dump(content, launch_file, indent=4)
                launch_file.truncate()
            except json.JSONDecodeError as e:
                reporter.error(
                    f"Parsing launch.json error at line {e.lineno}. Check documentation at https://ep-docs.embeff.com/#/DUT-Debugging?id=vscode"
                )
                exit(1)

    @staticmethod
    def send_command_to_vscode(command):
        """Ask the running VS Code instance to execute ``command`` (needs the Remote Control extension)."""
        try:
            from websockets.sync.client import connect as ws_connect
        except ImportError:
            reporter.error(
                "websocket module not available. Please install it.\nGo to https://ep-docs.embeff.com/#/DUT-Debugging?id=vscode for instructions."
            )
            exit(1)

        with ws_connect("ws://localhost:3711") as websocket:
            websocket.send('{ "command": "' + command + '" }')


class EPSession:
    """Framework-neutral ExecutionPlatform session.

    Owns the connection, configuration, lifecycle and DUT operations that are
    identical for every test framework.
    """

    class DebugMode(Enum):
        Off = 0
        External = 1
        VSCode = 2

    def __init__(self, url, config_file, tags=None, timeout_s=None):
        """Prepare a session.

        ``url`` is ``user:password@address`` (an EP or a central-management
        node), ``config_file`` the path to the ``.ep-config.zip``. ``tags`` narrows
        which EP central management may assign; ``timeout_s`` bounds how long to
        wait for a free EP. No connection is opened until ``connect()``.
        """
        self.init_Exception_str = None

        self.ep_config = ep_config(config_file)
        self.tags = tags or []
        self.timeout = float(timeout_s) if timeout_s is not None else None

        self.connection_parameters = URL_REGEX.search(url)
        self.connected = False

        self.ep_remote = None
        self.ep_address = None
        self.programmer_name = None

    # --- connection ---------------------------------------------------
    def connect(self):
        """Authenticate and open the connection to the EP.

        Exits the process on an unusable url or tag list. Central management may
        redirect to a different EP; ``ep_address`` reflects the one in use.
        """
        if not all(isinstance(tag, str) for tag in self.tags):
            reporter.error(f"ExecutionPlatform tags must all be of type str. Received: {self.tags!r}")
            self.close()
            exit(1)

        if self.connection_parameters is None:
            reporter.error(
                "ExecutionPlatform requires authentication. Specify the url in the form user:password@address. Please refer to https://ep-docs.embeff.com/#/EP_Config?id=the-robot-library for details."
            )
            self.close()
            exit(1)

        unresolved_addr = self.connection_parameters.group("address")
        ep_user = self.connection_parameters.group("user")
        ep_password = self.connection_parameters.group("password")

        self.ep_remote = Remote_WS(
            unresolved_addr, ep_user, ep_password, self.ep_config.dut_sku, self.tags, timeout=self.timeout
        )
        self.ep_address = (
            self.ep_remote.get_ep_address()
        )  # in case ep_address originally pointed to a management node, update address with address of assigned EP
        self.connected = True

    def close(self):
        """Release the EP, if this session holds it."""
        if self.connected:
            self.ep_remote.close()

    # --- remote keyword -----------------------------------------------
    def run_keyword(self, name, args, kwargs):
        """Run keyword ``name`` on the EP and return its value.

        Raises ``EPRemoteError`` if the keyword failed.
        """
        return self.ep_remote.run_keyword(name, args, kwargs)

    # --- configuration / compatibility --------------------------------
    def check_compatibility(self):
        """Verify the EP server's major/minor version matches this client."""
        try:
            ep_server_version = self.run_keyword("ep_get_version", [], {})
            client_match = SEMVER_REGEX.search(EP_CLIENT_VERSION)
            server_match = SEMVER_REGEX.search(ep_server_version)

            if client_match is None or server_match is None:
                raise RuntimeError(
                    f"Version compatibility check failed. EP client version: {EP_CLIENT_VERSION}. EP server version on {self.ep_address}: {ep_server_version}."
                )

            if (client_match.group("major") != server_match.group("major")) or (
                client_match.group("minor") != server_match.group("minor")
            ):
                raise RuntimeError(
                    f"Your EP client version {EP_CLIENT_VERSION} is incompatible with EP version {ep_server_version} on {self.ep_address}. Please update components so that major and minor version match."
                )

        except RuntimeError as e:  # Catch connection errors in case server is not available on given host. Remote errors (for example "ConfigurationMismatch") will not be caught.
            self.init_Exception_str = f"Failed to connect to {self.ep_address}: {e}"
            reporter.console(self.init_Exception_str)
            # It is not possible to directly raise an error, since the test execution is continued even though the frontend init fails. Instead, the error message
            # is stored until run_keyword() is executed. Test execution can then be stopped properly from within run_keyword().
            # This workaround only works for the keyword Dut Flash Firmware, since this is the only keyword known, when the connection to the remote server failed.

    def init_configuration(self):
        """Apply this session's ep-config to the EP and cache the programmer name."""
        self.load_configuration(self.ep_config.get_content())
        self.programmer_name = self.run_keyword("DUT ep_get_programmer", [], {})

    def load_configuration(self, configuration):
        """Activate ``configuration`` on the EP, uploading it if not already cached.

        Blocks while the EP is busy, up to the session timeout. If central
        management assigned the EP, a busy device triggers a new assignment
        instead of waiting.
        """
        newConfigHash = sha256(configuration)
        newConfigHash_str = newConfigHash.hexdigest()
        deadline = time.monotonic() + self.timeout if self.timeout is not None else None
        while True:
            try:
                configuration_found = self.run_keyword("ep_select_configuration", [newConfigHash_str], {})
                if not configuration_found:
                    self.run_keyword("ep_load_configuration", [configuration], {})
                break
            except DeviceBusy:
                if deadline is not None and time.monotonic() >= deadline:
                    reporter.error(
                        f"Timed out after {self.timeout}s waiting for ExecutionPlatform {self.ep_address} to become available."
                    )
                    exit(1)
                if self.ep_remote.was_redirected:
                    reporter.console(
                        f"ExecutionPlatform {self.ep_address} assigned by management is busy. Requesting a new assignment..."
                    )
                    self.close()
                    self.connected = False
                    self.connect()
                else:
                    reporter.console(f"ExecutionPlatform {self.ep_address} is currently in use. Waiting...")
                    sleep(2)
            except KeyboardInterrupt:
                exit(1)

    # --- lifecycle ----------------------------------------------------
    def suite_setup(self, name):
        """Start a suite named ``name`` (resets the FPGA and arms all endpoints)."""
        self.run_keyword("ep_suite_setup", [], {"name": name})

    def suite_teardown(self, name):
        """End the suite named ``name``."""
        self.run_keyword("ep_suite_teardown", [], {"name": name})

    def test_setup(self, name):
        """Start a test named ``name``."""
        self.run_keyword("ep_test_setup", [], {"name": name})

    def test_teardown(self, name):
        """End the test named ``name``.

        Raises ``EPRemoteError`` if the EP reports strict-mode violations or
        callback errors collected during the test.
        """
        self.run_keyword("ep_test_teardown", [], {"name": name})

    def enable_debugging(self):
        """Start the EP-side debug server."""
        self.run_keyword("DUT ep_enable_debugging", [], {})

    def disable_debugging(self):
        """Stop the EP-side debug server."""
        self.run_keyword("DUT ep_disable_debugging", [], {})

    def stop_debugging(self, debug_mode):
        """Tear down a debug session (counterpart to enabling it in suite setup)."""
        if debug_mode is self.DebugMode.Off:
            return
        if debug_mode is self.DebugMode.VSCode:
            VSCode_helper.send_command_to_vscode("workbench.action.debug.stop")
        self.disable_debugging()

    def is_system_test(self):
        """Return True if the ep-config has no System block, so no DUT communication exists."""
        return self.run_keyword("DUT ep_is_system_test", [], {})

    # --- debug mode ---------------------------------------------------
    def resolve_debug_mode(self, value):
        """Validate a raw ``ep_debugging`` value supplied by the frontend."""
        if value is None:
            return self.DebugMode.Off
        try:
            debugMode = self.DebugMode[value]
        except KeyError:
            reporter.error(f"Unsupported value for ep_debugging: '{value}'. Use: {[e.name for e in self.DebugMode]}.")
            exit(1)

        if debugMode == self.DebugMode.VSCode and not self.programmer_name.startswith("jlink"):
            reporter.error(
                f"ep_debugging is set to '{value}'. This is only supported when the ExecutionPlatform contains a JLink debugger. The selected EP ({self.ep_address}) does not. Use '{self.DebugMode.External.name}' mode instead."
            )
            exit(1)

        return debugMode

    # --- DUT operations -----------------------------------------------
    def flash_firmware(self, file, debug_mode):
        """Program ``file`` (``.bin`` or ``.hex``) onto the DUT and cold-start it.

        ``debug_mode`` is resolved by the frontend (see ``resolve_debug_mode``).
        When it is not ``Off`` the firmware is expected to be flashed by the
        debugger instead, so this only prepares the debug session and waits for
        the DUT. Flashing is skipped when the DUT already runs this firmware.

        Raises ``AssertionError`` for an unsupported file extension.
        """
        if debug_mode is not self.DebugMode.Off:
            if debug_mode is self.DebugMode.VSCode:
                VSCode_helper.update_launch(file, self.ep_address)
                # Remote debugging server is enabled in parallel on the EP.
                # Give it some time to start.
                sleep(1.0)
                VSCode_helper.send_command_to_vscode("workbench.action.debug.start")
            elif debug_mode is self.DebugMode.External:
                reporter.console(
                    f"Connect debugger to ExecutionPlatform, flash firmware and run it.\n- ExecutionPlatform address: {self.ep_address}\n- Expected file: {file}"
                )

            if self.is_system_test():
                reporter.console(
                    "System test mode: Test execution will continue. Use a test breakpoint to insert a pause here for debugger setup/flashing."
                )
            else:
                reporter.console("Waiting for DUT to start...")
                self.run_keyword("DUT ep_wait_until_app_loaded", [], {})

        else:
            _, file_extension = os.path.splitext(file)
            if file_extension != ".bin" and file_extension != ".hex":
                raise AssertionError("EndpointUsageError: DUT Flash Firmware only supports binary or hex files.")

            reporter.console("\nProgramming MCU...")
            with open(file, "rb") as firmware:
                firmware_contents = firmware.read()
                newFirmwareHash = sha256(firmware_contents)
                newFirmwareHash_str = newFirmwareHash.hexdigest()
                currentFirmwareHash_str = self.run_keyword("DUT ep_get_current_firmware_hash", [], {})
                if currentFirmwareHash_str is None or currentFirmwareHash_str != newFirmwareHash_str:
                    self.run_keyword("DUT ep_flash_firmware", [firmware_contents, file_extension], {})
                else:
                    self.run_keyword("DUT power_on", [], {})

    def get_coverage(self):
        """Invoke ``ep_dump_coverage`` on the DUT and return the raw binary dump.

        Raises ``AssertionError`` in system-test mode or if the dump fails.
        """
        if self.is_system_test():
            raise AssertionError(
                "`DUT Get Coverage` is not available: Missing System block in ep-config means DUT communication is not possible."
            )

        dump_res = self.run_keyword("DUT invoke", ["ep_dump_coverage"], {})
        if dump_res != 0:
            raise AssertionError(f"Error: Invoking `ep_dump_coverage` on DUT failed with return code '{dump_res}'.")

        return self.run_keyword("DUT ep_read_dumped_coverage", [], {})

    def start_model(self, file):
        """Upload the system model at ``file`` to the EP and start it."""
        filename = os.path.basename(file)
        with open(file, "rb") as model:
            model_contents = model.read()
            self.run_keyword(
                "SYSTEM ep_system_load_and_start_model", [filename, model_contents, self.ep_config.config_name], {}
            )

    def load_fmu(self, fmu_file):
        """Select the FMU at ``fmu_file`` on the EP, uploading and compiling it if new."""
        filename = os.path.basename(fmu_file)
        with open(fmu_file, "rb") as fmu:
            fmu_contents = fmu.read()

        hashsum = sha256(fmu_contents).hexdigest()
        fmu_ready = self.run_keyword("FMI ep_fmi_is_model_available", [hashsum], {})
        if fmu_ready:
            self.run_keyword("FMI ep_fmi_select_model", [hashsum], {})
            return

        reporter.console("\nTransferring and compiling FMU...")
        self.run_keyword("FMI ep_fmi_load_model", [filename, fmu_contents], {})


# ---------------------------------------------------------------------
# Endpoint functions that run on the PC instead of on the EP
# ---------------------------------------------------------------------
class KeywordCategory(Enum):
    """Section of the endpoint documentation a frontend function is listed in."""

    SETUP = "setup"
    CONTROLLING = "controlling"
    OBSERVING = "observing"


def keyword(category):
    """Mark a method of a ``FRONTEND_FUNCTIONS`` class as a test function of ``category``."""

    def decorator(function):
        function.category = category
        return function

    return decorator


class FrontendFunctions:
    """Functions of a static endpoint that the test frontend implements on top of ``EPSession``.

    ``debug_mode`` returns the debug mode at the time of the call; Robot Framework only
    knows it once a test runs.
    """

    def __init__(self, session, debug_mode):
        self._session = session
        self._debug_mode = debug_mode


class DUT_Functions(FrontendFunctions):
    @keyword(KeywordCategory.SETUP)
    def flash_firmware(self, file) -> None:
        """
        Programs the given firmware to the DUT. The firmware must be in binary `.bin` format or Intel HEX `.hex` format.

        - If a test suite uses the same firmware for all tests, you should use [Suite Setup](https://robotframework.org/robotframework/latest/RobotFrameworkUserGuide.html#suite-setup-and-teardown) to call `DUT Flash Firmware`.
        - If each test uses a different firmware, you can call `DUT Flash Firmware` directly from the test case.
        - The keyword will internally (1) enable the power supply (2) flash the DUT (3) cold-start the DUT.
        - In integration test mode (System block configured): waits until `ep_process_loop` is reached so that DUT is ready for processing.
        - In system test mode (no System block): skips the wait.
        - Minimize the use of flashing, as it increases test time.
        - After flashing a firmware the DUT is automatically powered on. If you need the same firmware for all tests but you require a cold-start on each test, then use `DUT Power Off` and `DUT Power On` in the [Test Setup](https://robotframework.org/robotframework/latest/RobotFrameworkUserGuide.html#test-setup-and-teardown).
        """
        self._session.flash_firmware(file, self._debug_mode())

    @keyword(KeywordCategory.OBSERVING)
    def get_coverage(self) -> bytes:
        """
        Invokes function ep_dump_coverage on DUT and read back the raw binary dump.
        You may need to further process the results - depending on the type of data.
        See for example `GCOV Process Coverage` keyword.
        """
        return self._session.get_coverage()


class SYSTEM_Functions(FrontendFunctions):
    @keyword(KeywordCategory.SETUP)
    def start_model(self, file) -> None:
        """
        - Loads the python file to the ExecutionPlatform.
        - If the python file is not conformant to the model structure mentioned above, an `EndpointUsageError` is raised.
        - The models `start` function is executed.
        """
        self._session.start_model(file)


class FMI_Functions(FrontendFunctions):
    @keyword(KeywordCategory.SETUP)
    def load_model(self, fmu_file) -> None:
        """
        - Uploads the FMU at path `fmu_file` to the ExecutionPlatform, compiles it if it is not cached yet, and maps its variables to the configured endpoints.
        - The model is not yet running afterwards.
        - Depending on the model, the compilation might take several minutes. It is cached by file content, so loading the same FMU again is fast.
        """
        self._session.load_fmu(fmu_file)


# The static endpoint each class adds its functions to.
FRONTEND_FUNCTIONS = {"DUT": DUT_Functions, "SYSTEM": SYSTEM_Functions, "FMI": FMI_Functions}


def frontend_functions(session, debug_mode):
    """Return the frontend functions bound to ``session`` per endpoint and method name,
    e.g. ``{"DUT": {"flash_firmware": ..., ...}, ...}``."""
    bound = {}
    for endpoint_name, functions_class in FRONTEND_FUNCTIONS.items():
        functions = functions_class(session, debug_mode)
        bound[endpoint_name] = {
            name: getattr(functions, name)
            for name, function in inspect.getmembers(functions_class, inspect.isfunction)
            if hasattr(function, "category")
        }
    return bound
