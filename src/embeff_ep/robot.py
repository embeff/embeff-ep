# Copyright (C) 2022 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

# Robot Framework frontend for the ExecutionPlatform.
#
# All framework-neutral behaviour (transport, connection, configuration,
# lifecycle and the DUT operations) lives in common.EPSession. This module
# only adapts that session to Robot Framework: the dynamic library API, the
# listener hooks, the ${ep_debugging} lookup and error rendering. It is the
# only EP client module that imports robotframework.
# Suites load it as the library EPLibrary (the top-level module EPLibrary).

import inspect
import posixpath
import re
from contextlib import contextmanager

from robot.libraries.BuiltIn import BuiltIn
from robot.errors import RemoteError
from robot.api import logger as robot_logger

from . import common
from .common import EPSession, EPRemoteError, frontend_functions, keyword_name


# Route the core's neutral reporter through Robot's logger so robot users keep
# the familiar log.html / console output.
class _RobotReporter:
    def error(self, msg):
        robot_logger.error(msg)

    def console(self, msg):
        robot_logger.console(msg)


common.reporter = _RobotReporter()


# Docstrings address pages and images the way the documentation pages do it
# themselves: relative to docs/Endpoints/, where every page carrying keyword
# documentation lives. VS Code renders keyword documentation as Markdown but has
# no base URI for it, so a relative target resolves to nothing: images show a
# broken-image icon and links lead nowhere. Rewrite them to the published
# documentation, pages in the docsify form the site serves them under.
# The generated endpoint typings need the same rewrite; the second copy lives in
# fpga/epconfig/epconfig_files/generate_typing.py.
DOCS_BASE_URL = "https://ep-docs.embeff.com/"
DOCS_KEYWORD_DIR = "Endpoints"
_MARKDOWN_TARGET = re.compile(r"(!?\[[^\]]*\]\()([^)]+)(\))")
_ALREADY_ABSOLUTE = re.compile(r"[a-z][a-z0-9+.-]*:|[#/]", re.IGNORECASE)


def _published_url(target):
    path, _, anchor = target.partition("#")
    path = posixpath.normpath(posixpath.join(DOCS_KEYWORD_DIR, path))
    if not path.endswith(".md"):
        return DOCS_BASE_URL + path
    return f"{DOCS_BASE_URL}#/{path[: -len('.md')]}" + (f"?id={anchor}" if anchor else "")


def _absolute_docs_urls(documentation):
    def rewrite(match):
        target = match.group(2)
        if _ALREADY_ABSOLUTE.match(target):
            return match.group(0)
        return match.group(1) + _published_url(target) + match.group(3)

    return _MARKDOWN_TARGET.sub(rewrite, documentation)


# Translate the transport's neutral error into Robot's RemoteError.
@contextmanager
def _as_robot_error():
    try:
        yield
    except EPRemoteError as e:
        raise RemoteError(e.message, e.traceback, e.fatal, e.continuable) from None


class EPLibrary:
    """Robot library to access ExecutionPlatform functionality.

    The constructor expects EP address and ep-config. The library needs this information
    to provide code-completion for all keywords (available keywords depend on the configuration).
    Robot variables are not yet available at that moment.

    At runtime the global robot variable ${ep_debugging} is used to turn on/off debugging.
    """

    ROBOT_LIBRARY_SCOPE = "SUITE"
    ROBOT_LISTENER_API_VERSION = 3

    # Keyword documentation is written as Markdown, but Robot Framework has no
    # Markdown documentation format. Any other format then
    # ROBOT and reST (such as TEXT) is passed through verbatim into the Markdown of the hover
    # and auto-complete popups, which is what renders tables and pictures there.
    ROBOT_LIBRARY_DOC_FORMAT = "TEXT"

    DebugMode = EPSession.DebugMode

    def __init__(self, url, config_file, tags=None, timeout_s=None):
        self.session = EPSession(url, config_file, tags, timeout_s)
        self.frontend_functions = {
            keyword_name(endpoint_name, name): function  # "DUT flash_firmware", ...
            for endpoint_name, functions in frontend_functions(self.session, self.get_debug_mode).items()
            for name, function in functions.items()
        }

        self.ROBOT_LIBRARY_LISTENER = self
        self.suite_setup_failed = False
        self.test_setup_failed = False

    # ------------------------------------------------------------------
    # Robot dynamic library API
    # ------------------------------------------------------------------
    def run_keyword(self, name, args, kwargs):
        if self.session.init_Exception_str is not None:
            BuiltIn().fatal_error(self.session.init_Exception_str)
        with _as_robot_error():
            if name in self.session.ep_config.get_keyword_names():
                return self.session.run_keyword(name, args, kwargs)
            else:
                return self.frontend_functions[name](*args, **kwargs)

    def get_keyword_names(self):
        return self.session.ep_config.get_keyword_names() + list(self.frontend_functions)

    def get_keyword_arguments(self, name):
        if name in self.session.ep_config.get_keyword_names():
            return self.session.ep_config.get_keyword_arguments(name)

        kw = self.frontend_functions[name]
        args, varargs, kwargs, defaults, *_ = inspect.getfullargspec(kw)
        if inspect.ismethod(kw):
            args = args[1:]  # drop 'self'
        if defaults:
            args, names = args[: -len(defaults)], args[-len(defaults) :]
            args += ["%s=%s" % (n, d) for n, d in zip(names, defaults, strict=True)]
        if varargs:
            args.append("*%s" % varargs)
        if kwargs:
            args.append("**%s" % kwargs)
        return args

    def get_keyword_documentation(self, name):
        if name == "__intro__":
            return ""
        if name in self.session.ep_config.get_keyword_names():
            return _absolute_docs_urls(self.session.ep_config.get_keyword_documentation(name) or "")

        kw = self.frontend_functions[name]
        return _absolute_docs_urls(inspect.getdoc(kw) or "")

    def get_keyword_tags(self, name):
        if name in self.session.ep_config.get_keyword_names():
            return self.session.ep_config.get_keyword_tags(name)

        kw = self.frontend_functions[name]
        return getattr(kw, "robot_tags", [])

    # ------------------------------------------------------------------
    # Robot-specific input: the ${ep_debugging} variable
    # ------------------------------------------------------------------
    def get_debug_mode(self):
        val = BuiltIn().get_variable_value("${ep_debugging}", default=self.DebugMode.Off.name)
        return self.session.resolve_debug_mode(val)

    # ------------------------------------------------------------------
    # Robot listener hooks
    # ------------------------------------------------------------------
    def start_suite(self, data, result):
        try:
            if not self.session.connected:
                self.session.connect()
            self.session.init_configuration()

            self.session.check_compatibility()

            self.session.suite_setup(data.longname)

            if self.get_debug_mode() is not self.DebugMode.Off:
                self.session.enable_debugging()

        except Exception as e:
            self.suite_setup_failed = True
            robot_logger.error(str(e))
            exit(1)

        self.suite_setup_failed = False

    def end_suite(self, data, result):
        try:
            with _as_robot_error():
                self.session.suite_teardown(data.longname)
                self.session.stop_debugging(self.get_debug_mode())
        finally:
            if (
                self.suite_setup_failed and self.session.init_Exception_str is None
            ):  # when init failed, result.passed cannot be modified: "AttributeError: can't set attribute 'passed'"
                result.passed = False

    def start_test(self, data, result):
        try:
            with _as_robot_error():
                self.session.test_setup(data.longname)
        except Exception:
            self.test_setup_failed = True
            raise
        self.test_setup_failed = False

    def end_test(self, data, result):
        try:
            with _as_robot_error():
                self.session.test_teardown(data.longname)
        except RemoteError as e:
            result.passed = False
            result.message = f"Test teardown failed.\n{e!s}"
        except Exception as e:
            raise e
        finally:
            if self.test_setup_failed:
                result.passed = False

    def close(self):
        self.session.close()
