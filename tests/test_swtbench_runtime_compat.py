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
    _configure_container_reuse,
    _container_is_reusable,
    _decode_test_output,
    _lock_filename,
    _make_tree_world_accessible,
    _retryable_build_failure,
)
from brt6.runtime.swt_cached_compat import offline_eval_commands


class SWTBenchRuntimeCompatibilityTests(unittest.TestCase):
    def test_lock_name_uses_instance_id_without_defining_a_container_name(self) -> None:
        self.assertEqual(
            _lock_filename("astropy__astropy-7746"),
            "astropy__astropy-7746.lock",
        )

    def test_container_reuse_environment_uses_root_and_instance_scope(self) -> None:
        environment = {}

        _configure_container_reuse(environment)

        self.assertEqual(environment["SWT_REUSE_CONTAINERS"], "1")
        self.assertEqual(environment["SWT_KEEP_CONTAINERS"], "1")
        self.assertEqual(environment["SWT_CONTAINER_REUSE_SCOPE"], "instance")
        self.assertEqual(environment["SWT_SKIP_EVAL_INSTALL"], "1")
        self.assertEqual(
            environment["BRT_SWT_CONTAINER_LOCK_DIR"],
            "/root/Baxxhy/BugReproduce/brt6/.runtime/locks",
        )

    def test_reuse_rejects_wrong_image_or_broken_state(self) -> None:
        healthy = {
            "Config": {"Image": "exec.eval.expected:latest"},
            "State": {"Status": "running", "Dead": False},
        }
        self.assertTrue(_container_is_reusable(healthy, "exec.eval.expected:latest"))
        self.assertFalse(_container_is_reusable(healthy, "exec.eval.other:latest"))
        self.assertFalse(
            _container_is_reusable(
                {
                    "Config": {"Image": "exec.eval.expected:latest"},
                    "State": {"Status": "dead", "Dead": True},
                },
                "exec.eval.expected:latest",
            )
        )

    def test_cached_eval_removes_every_runtime_install_and_keeps_state_commands(self) -> None:
        commands = [
            "source /opt/miniconda3/bin/activate",
            "git status",
            "git show",
            "git diff base123",
            "python -m pip install -e .[test] --verbose",
            "python -c 'import roman' || python -m pip install roman==3.3",
            "git apply -v -",
            "python -m pytest tests/test_generated.py",
            "git checkout base123",
        ]

        converted = offline_eval_commands(commands, base_commit="base123")

        rendered = "\n".join(converted)
        self.assertEqual(
            converted[0],
            "export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
            "HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 TRANSFORMERS_OFFLINE=1",
        )
        self.assertNotIn("pip install", rendered)
        self.assertNotIn("git status", converted)
        self.assertNotIn("git show", converted)
        self.assertNotIn("git diff base123", converted)
        self.assertIn("git apply -v -", converted)
        self.assertIn("python -m pytest tests/test_generated.py", converted)
        self.assertIn("git checkout base123", converted)

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
            self.assertEqual(
                set(diagnostic),
                {
                    "schema_version",
                    "policy",
                    "raw_bytes",
                    "error_start",
                    "error_end",
                    "error_reason",
                    "invalid_bytes_hex",
                },
            )

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
