from __future__ import annotations

import json
import logging.handlers
import os
import tempfile
import unittest
from pathlib import Path

from brt6.evaluation import swtbench_runtime_compat as runtime_compat
from brt6.evaluation.swtbench_runtime_compat import (
    _bounded_setup_logger,
    _decode_test_output,
    _make_tree_world_accessible,
    _retryable_build_failure,
)


class SWTBenchRuntimeCompatibilityTests(unittest.TestCase):
    def test_cached_image_cleanup_is_a_noop(self) -> None:
        remover = getattr(runtime_compat, "_preserve_cached_image", None)
        self.assertIsNotNone(remover)

        class Images:
            def remove(self, *_args, **_kwargs):
                raise AssertionError("shared cached image must not be removed")

        class Client:
            images = Images()

        self.assertIsNone(
            remover(Client(), "exec.eval.x86_64.cached:latest", "quiet")
        )

    def test_valid_utf8_is_unchanged_and_has_no_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            raw = "PASSED 中文\n".encode("utf-8")

            self.assertEqual(_decode_test_output(raw, directory), "PASSED 中文\n")
            self.assertFalse((directory / "decode_diagnostics.json").exists())

    def test_invalid_bytes_are_escaped_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            decoded = _decode_test_output(b"PASSED\n\xd8\xff\nFAILED\n", directory)

            self.assertEqual(decoded, "PASSED\n\\xd8\\xff\nFAILED\n")
            diagnostic = json.loads(
                (directory / "decode_diagnostics.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                diagnostic["policy"], "utf-8-strict-then-backslashreplace"
            )
            self.assertEqual(diagnostic["invalid_bytes_hex"], "d8")
            self.assertEqual(diagnostic["raw_bytes"], 17)
            self.assertEqual(len(diagnostic["raw_sha256"]), 64)

    def test_logger_is_bounded_and_records_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "official.log"
            logger = _bounded_setup_logger("instance", path, "w")
            try:
                self.assertIsInstance(
                    logger.handlers[-1], logging.handlers.RotatingFileHandler
                )
                self.assertEqual(logger.log_file, path)
                self.assertGreater(logger.handlers[-1].maxBytes, 0)
            finally:
                for handler in list(logger.handlers):
                    handler.close()
                    logger.removeHandler(handler)

    def test_staged_tree_matches_official_recursive_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            nested = root / ".git" / "objects"
            nested.mkdir(parents=True)
            file_path = nested / "object"
            file_path.write_text("content", encoding="utf-8")
            os.chmod(root, 0o700)
            os.chmod(nested, 0o700)
            os.chmod(file_path, 0o600)

            _make_tree_world_accessible(root)

            self.assertEqual(root.stat().st_mode & 0o777, 0o777)
            self.assertEqual(nested.stat().st_mode & 0o777, 0o777)
            self.assertEqual(file_path.stat().st_mode & 0o777, 0o777)

    def test_build_retry_only_accepts_known_infrastructure_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            log = directory / "build_image.log"
            log.write_text(
                "astropy_helpers: Failed to connect to github.com port 443",
                encoding="utf-8",
            )
            self.assertTrue(_retryable_build_failure(directory))
            log.write_text(
                "subprocess.TimeoutExpired: git status timed out after 40 seconds",
                encoding="utf-8",
            )
            self.assertTrue(_retryable_build_failure(directory))
            log.write_text(
                "No matching distribution found for pip==25.2", encoding="utf-8"
            )
            self.assertFalse(_retryable_build_failure(directory))
            log.write_text(
                "Installing build dependencies: finished with status 'error'\n"
                "ResolutionImpossible\n"
                "some packages have no matching distributions available: setuptools",
                encoding="utf-8",
            )
            self.assertTrue(_retryable_build_failure(directory))


if __name__ == "__main__":
    unittest.main()
