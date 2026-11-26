# Copyright (C) 2026 embeff GmbH
# SPDX-License-Identifier: Apache-2.0
# This file is part of the ExecutionPlatform distribution. More information at https://embeff.com

import argparse
import io
import os
import tempfile
import unittest
import zipfile

from embeff_ep.cli import ep_config


def make_ep_config(dut_sku=b"DUT_STM32U5_D1", ep_version=b"9.2.0", config_yaml=b"System:\n  UART:\n    DUT_TX: 1\n"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("dut-sku", dut_sku)
        archive.writestr("ep-version", ep_version)
        archive.writestr("config.yaml", config_yaml)
    return buffer.getvalue()


class TestInfo(unittest.TestCase):
    def test_reads_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".ep-config.zip", delete=False) as f:
            f.write(make_ep_config())
            path = f.name

        try:
            rc = ep_config.cmd_info(argparse.Namespace(file=path))
            self.assertEqual(rc, 0)
        finally:
            os.remove(path)

    def test_missing_part_reports_error(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("dut-sku", b"DUT_STM32U5_D1")
            # ep-version and config.yaml intentionally missing

        with tempfile.NamedTemporaryFile(suffix=".ep-config.zip", delete=False) as f:
            f.write(buffer.getvalue())
            path = f.name

        try:
            rc = ep_config.cmd_info(argparse.Namespace(file=path))
            self.assertEqual(rc, 2)
        finally:
            os.remove(path)

    def test_not_a_zip_reports_error(self):
        with tempfile.NamedTemporaryFile(suffix=".ep-config.zip", delete=False) as f:
            f.write(b"not a zip file")
            path = f.name

        try:
            rc = ep_config.cmd_info(argparse.Namespace(file=path))
            self.assertEqual(rc, 2)
        finally:
            os.remove(path)


class TestErrorMessage(unittest.TestCase):
    def test_401_has_actionable_message(self):
        message = ep_config.error_message(401, {"detail": "Invalid or disabled API secret."})
        self.assertIn("EP_CONFIG_SECRET", message)

    def test_detail_passthrough(self):
        message = ep_config.error_message(404, {"detail": "Unknown or inaccessible DUT 'FOO'."})
        self.assertEqual(message, "Unknown or inaccessible DUT 'FOO'.")

    def test_redirect_points_at_portal_url(self):
        # The give-away for a portal URL aimed at something that is not this API: that server
        # answers the POST with a redirect to its own login page.
        message = ep_config.error_message(307, "", "https://customer.embeff.com")
        self.assertIn("https://customer.embeff.com", message)
        self.assertIn("EP_CONFIG_PORTAL_URL", message)

    def test_unexpected_response_names_the_host(self):
        message = ep_config.error_message(502, "<html>bad gateway</html>", "http://localhost:8080")
        self.assertIn("http://localhost:8080", message)


if __name__ == "__main__":
    unittest.main()


class TestBuildOutput(unittest.TestCase):
    """The artifact's name is fixed at build time: the client imports the typings embedded under that
    name, so the downloaded file must be named after the input rather than freely chosen.
    """

    def setUp(self):
        self.calls = []
        self.original_request = ep_config.request

        def fake_request(portal_url, path, secret, method="GET", body=None):
            self.calls.append(path)
            if path == "/api/v1/builds":
                return 202, {"build_uuid": "uuid-1"}
            if path == "/api/v1/builds/uuid-1":
                return 200, {"status": "Ready", "failure_text": None}
            return 200, b"artifact-bytes"

        ep_config.request = fake_request

    def tearDown(self):
        ep_config.request = self.original_request

    def build_args(self, input_path, output_folder):
        return argparse.Namespace(
            input=input_path,
            output_folder=output_folder,
            dut="DUT_STM32U5_D1",
            version="9.7.0",
            secret="s",
            portal_url="http://portal",
            poll_interval=0,
            timeout=60,
        )

    def test_names_result_after_the_input_file(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "my_config.yaml")
            with open(source, "w") as f:
                f.write("System:\n")
            destination = os.path.join(folder, "out")
            os.mkdir(destination)

            self.assertEqual(ep_config.cmd_build(self.build_args(source, destination)), 0)

            written = os.path.join(destination, "my_config.ep-config.zip")
            self.assertTrue(os.path.exists(written), os.listdir(destination))
            with open(written, "rb") as f:
                self.assertEqual(f.read(), b"artifact-bytes")

    def test_builds_under_the_same_name_it_writes(self):
        # The name sent to the portal is baked into the artifact, so it has to match the file name
        # written locally - otherwise the client rejects the result it just downloaded.
        sent = {}
        self.original_request = ep_config.request

        def capture(portal_url, path, secret, method="GET", body=None):
            if path == "/api/v1/builds":
                sent.update(body)
                return 202, {"build_uuid": "uuid-1"}
            if path == "/api/v1/builds/uuid-1":
                return 200, {"status": "Ready", "failure_text": None}
            return 200, b"artifact-bytes"

        ep_config.request = capture
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "gpio.yaml")
            with open(source, "w") as f:
                f.write("System:\n")

            ep_config.cmd_build(self.build_args(source, folder))
            written = os.listdir(folder)

        self.assertEqual(sent["name"], "gpio")
        self.assertIn("gpio.ep-config.zip", written)

    def test_schema_suffix_is_not_part_of_the_name(self):
        # VS Code applies the schema to *.ep-config.yaml; the portal rejects dots in the name.
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "my_config.ep-config.yaml")
            with open(source, "w") as f:
                f.write("System:\n")

            self.assertEqual(ep_config.cmd_build(self.build_args(source, folder)), 0)
            self.assertTrue(os.path.exists(os.path.join(folder, "my_config.ep-config.zip")), os.listdir(folder))

    def test_missing_output_folder_is_rejected_before_queueing(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "my_config.yaml")
            with open(source, "w") as f:
                f.write("System:\n")

            self.assertEqual(ep_config.cmd_build(self.build_args(source, os.path.join(folder, "absent"))), 2)

        self.assertEqual(self.calls, [], "no build may be queued when the folder is wrong")
