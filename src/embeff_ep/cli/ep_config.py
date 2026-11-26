# Copyright (C) 2026 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

"""Command line access to the ExecutionPlatform customer portal's validate/build workflow,
and offline inspection of existing .ep-config.zip files.

'validate' and 'build' need an API secret. Enable API access and read the secret via
'API access' in the customer portal (https://customer.embeff.com).
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile


DEFAULT_PORTAL_URL = "https://customer.embeff.com"
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_TIMEOUT = 35 * 60.0


def get_portal_url():
    return os.environ.get("EP_CONFIG_PORTAL_URL", DEFAULT_PORTAL_URL)


def get_secret(args):
    secret = args.secret or os.environ.get("EP_CONFIG_SECRET")
    if not secret:
        print(
            "error: no API secret provided. Set the EP_CONFIG_SECRET environment variable "
            "(preferred) or pass --secret.",
            file=sys.stderr,
        )
        sys.exit(2)
    return secret


def request(portal_url, path, secret, method="GET", body=None):
    url = portal_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {secret}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                return response.status, json.loads(raw)
            return response.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode("utf-8", "replace")
    except urllib.error.URLError as e:
        print(f"error: could not reach {portal_url}: {e.reason}", file=sys.stderr)
        sys.exit(2)


def error_message(status, payload, portal_url=None):
    if status == 401:
        return "Authentication failed. Check EP_CONFIG_SECRET (see 'API access' in the customer portal)."
    if isinstance(payload, dict) and "detail" in payload:
        return payload["detail"]
    if 300 <= status < 400:
        # Typically EP_CONFIG_PORTAL_URL points somewhere that is not this API at all, and the
        # redirect is that server sending us to its login page.
        return (
            f"{portal_url} redirected the request ({status}) instead of answering it, so it "
            "does not serve the ep-config API. Check EP_CONFIG_PORTAL_URL if you set it."
        )
    return f"Unexpected response ({status}) from {portal_url}: {payload}"


def cmd_validate(args):
    secret = get_secret(args)
    with open(args.input, "r") as f:
        yaml_content = f.read()

    status, payload = request(
        args.portal_url,
        "/api/v1/validate",
        secret,
        method="POST",
        body={"dut": args.dut, "ep_version": args.version, "yaml": yaml_content},
    )

    if status == 200 and payload.get("valid"):
        print("Configuration is valid.")
        return 0
    if status == 200:
        print(f"Configuration is invalid: {payload.get('error')}", file=sys.stderr)
        return 1

    print(f"error: {error_message(status, payload, args.portal_url)}", file=sys.stderr)
    return 2


def cmd_build(args):
    secret = get_secret(args)
    with open(args.input, "r") as f:
        yaml_content = f.read()

    if not os.path.isdir(args.output_folder):
        print(f"error: output folder does not exist: {args.output_folder}", file=sys.stderr)
        return 2

    name = os.path.splitext(os.path.basename(args.input))[0].removesuffix(".ep-config")
    status, payload = request(
        args.portal_url,
        "/api/v1/builds",
        secret,
        method="POST",
        body={"dut": args.dut, "ep_version": args.version, "yaml": yaml_content, "name": name},
    )
    if status != 202:
        print(f"error: {error_message(status, payload, args.portal_url)}", file=sys.stderr)
        return 2

    build_uuid = payload["build_uuid"]
    print(f"Build queued ({build_uuid}).")

    deadline = time.monotonic() + args.timeout
    while True:
        status, payload = request(args.portal_url, f"/api/v1/builds/{build_uuid}", secret)
        if status != 200:
            print(f"error: {error_message(status, payload, args.portal_url)}", file=sys.stderr)
            return 2

        build_status = payload["status"]
        if build_status == "Ready":
            break
        if build_status == "Failed":
            print(f"error: build failed: {payload.get('failure_text')}", file=sys.stderr)
            return 1
        if time.monotonic() > deadline:
            print(f"\nerror: timed out waiting for build after {args.timeout}s.", file=sys.stderr)
            return 2

        print(".", end="", flush=True)
        time.sleep(args.poll_interval)

    print("\nBuild finished, downloading...")
    status, content = request(args.portal_url, f"/api/v1/builds/{build_uuid}/download", secret)
    if status != 200:
        print(f"error: {error_message(status, content, args.portal_url)}", file=sys.stderr)
        return 2

    output_path = os.path.join(args.output_folder, f"{name}.ep-config.zip")
    with open(output_path, "wb") as f:
        f.write(content)
    print(f"Wrote {output_path}")
    return 0


def cmd_info(args):
    try:
        with zipfile.ZipFile(args.file, "r") as archive:
            dut_sku = archive.read("dut-sku").decode("ascii")
            ep_version = archive.read("ep-version").decode("ascii")
            config_yaml = archive.read("config.yaml").decode("utf-8")
    except KeyError as e:
        print(f"error: '{args.file}' is missing required part {e}. Check EP version compatibility.", file=sys.stderr)
        return 2
    except (zipfile.BadZipFile, FileNotFoundError) as e:
        print(f"error: could not read '{args.file}': {e}", file=sys.stderr)
        return 2

    print(f"DUT:     {dut_sku}")
    print(f"Version: {ep_version}")
    print("YAML:")
    print(config_yaml)
    return 0


def add_portal_args(sub):
    sub.set_defaults(portal_url=get_portal_url())
    sub.add_argument("--secret", help="API secret. Prefer the EP_CONFIG_SECRET environment variable over this flag.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ep-config",
        description="Validate, build and inspect ep-config files via the ExecutionPlatform customer portal.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate", help="Check whether a configuration is valid for a DUT/EP version (online check)."
    )
    validate.add_argument("--dut", required=True)
    validate.add_argument("--version", required=True)
    validate.add_argument("input")
    add_portal_args(validate)
    validate.set_defaults(func=cmd_validate)

    build = subparsers.add_parser(
        "build",
        help="Build a configuration and download the resulting ep-config file. Blocks until the build finishes.",
    )
    build.add_argument("--dut", required=True)
    build.add_argument("--version", required=True)
    build.add_argument(
        "--output-folder",
        required=True,
        help="Folder to write <input>.ep-config.zip into. The name is fixed at build time and must not be changed afterwards.",
    )
    build.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help=argparse.SUPPRESS)
    build.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="Give up waiting for the build after this many seconds."
    )
    build.add_argument("input")
    add_portal_args(build)
    build.set_defaults(func=cmd_build)

    info = subparsers.add_parser(
        "info", help="Show DUT, version and YAML content of an existing ep-config file (offline, no API secret needed)."
    )
    info.add_argument("file")
    info.set_defaults(func=cmd_info)

    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
