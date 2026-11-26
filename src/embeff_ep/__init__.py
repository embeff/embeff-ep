# Copyright (C) 2026 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

"""Python client for the ExecutionPlatform: Robot Framework library, pytest plugin and ep-config CLI."""

__version__ = "9.8.0"

from .common import EPRemoteError  # after __version__, which common reads

__all__ = ["EPRemoteError", "__version__"]
