# Copyright (C) 2026 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

# pytest frontend for the ExecutionPlatform.
#
# For more details see https://ep-docs.embeff.com/#/CodeInterface

import os
import sys
from functools import wraps
import inspect
from typing import TYPE_CHECKING, Literal

import pytest

from . import common
from .common import EPSession, EPRemoteError, frontend_functions  # EPRemoteError is also re-exported for users


__tracebackhide__ = True


# Override default reporter
class _PytestReporter:
    def __init__(self):
        self.last_error = None

    def error(self, msg):
        self.last_error = msg
        print(msg, file=sys.stderr)

    def console(self, msg):
        print(msg)


_reporter = _PytestReporter()
common.reporter = _reporter


# ---------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------
def _short_ep_traceback(call):
    """Shroten the error traceback."""

    @wraps(call)
    def wrapper(*args, **kwargs):
        try:
            return call(*args, **kwargs)
        except EPRemoteError as e:
            raise EPRemoteError(e.message, e.traceback, e.fatal, e.continuable) from None

    return wrapper


def _shorten_endpoint_tracebacks(configuration):
    """Route every endpoint keyword of ``configuration`` through the wrapper."""
    for endpoint in vars(configuration).values():
        for name, keyword in vars(endpoint).items():
            if callable(keyword):
                setattr(endpoint, name, _short_ep_traceback(keyword))


def _add_frontend_functions(configuration, session, debug_mode):
    """Add the functions of ``common.FRONTEND_FUNCTIONS`` to their live endpoint, e.g. ``ep.DUT.flash_firmware``."""
    for endpoint_name, functions in frontend_functions(session, lambda: debug_mode).items():
        for name, function in functions.items():
            setattr(getattr(configuration, endpoint_name), name, function)


# ---------------------------------------------------------------------
# EP handle exposed to tests
# ---------------------------------------------------------------------
class EP:
    """The handle to one EP. Each configuration derives its own class, ``<configuration>.test.EP``."""

    CONFIG_PATH = None  # The .ep-config the subclass of a configuration opens.

    @classmethod
    def fixture(
        cls, url=None, *, debugging: Literal["Off", "External", "VSCode"] | None = None, tags=None, timeout_s=None
    ):
        """Return a module-scoped pytest fixture that opens an EP with this configuration.

        Assign it to the name the tests request, and annotate them with the same class:

            from gpio.test import EP

            ep = EP.fixture()

            def test_x(ep: EP): ...

        ``url`` is ``user:password@address`` of an EP or of a central-management node.
        It falls back to $EP_URL when omitted. If $EP_URL is empty as well, the run stops.
        ``debugging`` is ``Off``, ``External`` or ``VSCode``.
        It falls back to $EP_DEBUGGING when omitted. If $EP_DEBUGGING is empty as well, it falls back to ``Off``.
        ``tags`` narrows which EP central management may assign.
        ``timeout_s`` bounds how long to wait for a free EP.
        It falls back to $EP_TIMEOUT_S when omitted. If $EP_TIMEOUT_S is empty as well, there is no timeout.

        The EP opens before the first test of the module that needs it and closes when the module ends.
        """
        if cls.CONFIG_PATH is None:
            raise TypeError("Use the EP class of a configuration, e.g. gpio.test.EP, not embeff_ep.pytest_plugin.EP.")

        @pytest.fixture(scope="module")
        def _ep_fixture(request):
            yield from _open_ep(request, cls, url, debugging, tags, timeout_s)

        return _ep_fixture

    def __init__(self, session, debug_mode):
        self.session = session
        self._endpoints = session.ep_config.build_live_configuration(
            session.run_keyword
        )  # ep.DUT.invoke(...), ep.CAN1.can_send(...), ...
        _add_frontend_functions(self._endpoints, session, debug_mode)
        _shorten_endpoint_tracebacks(self._endpoints)

    if not TYPE_CHECKING:
        # The type checker must not see this method. A class with __getattr__ makes every
        # attribute name valid, and the checker would then miss a misspelled endpoint.
        def __getattr__(self, name):
            """Return the endpoint called ``name``, so a test can write ``ep.DUT``."""
            endpoints = self.__dict__.get("_endpoints")
            if endpoints is None or name.startswith("_"):
                raise AttributeError(name)
            try:
                return getattr(endpoints, name)
            except AttributeError:
                raise AttributeError(
                    f"The EP handle has no '{name}'. This configuration provides the endpoints "
                    f"{', '.join(sorted(vars(endpoints)))}."
                ) from None


# ---------------------------------------------------------------------
# Opening an EP
# ---------------------------------------------------------------------
_HANDLES = {}  # (module name, fixture name)


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef, request):
    """Fixtures get the same check of their annotated EP arguments as tests."""
    _check_handle_annotations(fixturedef.func, fixturedef.argnames, request.getfixturevalue)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    _check_handle_annotations(pyfuncitem.obj, pyfuncitem.funcargs, pyfuncitem.funcargs.__getitem__)


def _check_handle_annotations(function, argnames, value_of):
    """Fail if an argument annotated with the EP class of one configuration receives the handle of another.

    The annotation drives code completion, the fixture decides what runs. This keeps the two in line.
    """
    try:
        annotations = inspect.get_annotations(function, eval_str=True)
    except Exception:
        return  # An annotation that does not evaluate cannot name an EP class.
    for name, annotation in annotations.items():
        if not (isinstance(annotation, type) and issubclass(annotation, EP)) or name not in argnames:
            continue
        handle = value_of(name)
        if isinstance(handle, EP) and not isinstance(handle, annotation):
            pytest.fail(
                f"'{name}' of {function.__name__}() is annotated with {annotation.__name__}, "
                f"but its fixture provides {type(handle).__name__}. Code completion would show "
                f"other endpoints than the test gets. Annotate it with the EP class the fixture uses.",
                pytrace=False,
            )


# ---------------------------------------------------------------------
# Inputs that may come from the environment
# ---------------------------------------------------------------------
EP_URL_VARIABLE = "EP_URL"
EP_DEBUGGING_VARIABLE = "EP_DEBUGGING"
EP_TIMEOUT_VARIABLE = "EP_TIMEOUT_S"


def _resolve(value, variable):
    """Return ``value`` and where it comes from."""
    if value is not None:
        return value, "the argument of EP.fixture()"
    from_environment = os.environ.get(variable, "").strip()
    return from_environment or None, f"${variable}"


def _resolve_url(url):
    url, _ = _resolve(url, EP_URL_VARIABLE)
    if url is None:
        pytest.fail(
            "No EP url specified. Specify one in EP.fixture():\n"
            '    ep = EP.fixture("user:password@address")\n'
            f"Or set the environment variable ${EP_URL_VARIABLE} to 'user:password@address'.",
            pytrace=False,
        )
    return url


def _resolve_debugging(debugging):
    debugging, source = _resolve(debugging, EP_DEBUGGING_VARIABLE)
    if debugging is None:
        return EPSession.DebugMode.Off.name
    if debugging not in EPSession.DebugMode.__members__:
        pytest.fail(
            f"Unsupported value for debugging: '{debugging}' (from {source}). "
            f"Use one of {[mode.name for mode in EPSession.DebugMode]}.",
            pytrace=False,
        )
    return debugging


def _resolve_timeout(timeout_s):
    timeout_s, source = _resolve(timeout_s, EP_TIMEOUT_VARIABLE)
    if timeout_s is None:
        return None
    try:
        return float(timeout_s)
    except (TypeError, ValueError):
        pytest.fail(
            f"timeout_s must be a number of seconds. Received '{timeout_s}' (from {source}). "
            f"Leave it out, or clear ${EP_TIMEOUT_VARIABLE}, to wait without a limit.",
            pytrace=False,
        )


def _open_ep(request, handle_class, url, debugging, tags, timeout_s):
    """Connect to the EP, run suite_setup, yield the handle, and run suite_teardown at the end of the module."""
    url = _resolve_url(url)
    debugging = _resolve_debugging(debugging)
    timeout_s = _resolve_timeout(timeout_s)

    suite_name = request.module.__name__
    handle_name = request.fixturename
    session = EPSession(url, handle_class.CONFIG_PATH, tags, timeout_s)
    aborted = None

    try:
        session.connect()

        # Prevent a busy-deadlock caused by two handles of one test module resolving to the same EP.
        for (module, name), other in _HANDLES.items():
            if module == suite_name and other.session.ep_address == session.ep_address:
                pytest.fail(
                    f"The fixtures '{name}' and '{handle_name}' both use the same EP at {session.ep_address}.",
                    pytrace=False,
                )

        session.init_configuration()
        session.check_compatibility()
        if session.init_Exception_str is not None:
            pytest.fail(session.init_Exception_str, pytrace=False)

        debug_mode = session.resolve_debug_mode(debugging)
        handle = handle_class(session, debug_mode)
        session.suite_setup(suite_name)
        if debug_mode is not session.DebugMode.Off:
            session.enable_debugging()
    except SystemExit:
        # Fail outside this handler, to keep pytest error short.
        session.close()
        aborted = f"{handle_name}: {_reporter.last_error or 'cannot open the EP.'}"
    except BaseException:
        session.close()
        raise

    if aborted is not None:
        pytest.fail(aborted, pytrace=False)

    _HANDLES[(suite_name, handle_name)] = handle
    try:
        yield handle
    finally:
        del _HANDLES[(suite_name, handle_name)]
        try:
            session.suite_teardown(suite_name)
            session.stop_debugging(debug_mode)
        finally:
            session.close()


# ---------------------------------------------------------------------
# Per-test lifecycle
# ---------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _ep_test_lifecycle(request):
    """Run ep_test_setup before and ep_test_teardown after every test that uses an EP."""
    if not _HANDLES:
        yield
        return

    module = getattr(getattr(request, "module", None), "__name__", None)
    handles = [_HANDLES[(module, name)] for name in request.fixturenames if (module, name) in _HANDLES]

    test_name = request.node.name
    for handle in handles:
        _test_setup(handle, test_name)
    yield
    for handle in handles:
        _test_teardown(handle, test_name)


@_short_ep_traceback
def _test_setup(handle, test_name):
    handle.session.test_setup(test_name)


@_short_ep_traceback
def _test_teardown(handle, test_name):
    handle.session.test_teardown(test_name)
