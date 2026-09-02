from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brt6.core.ablation import AblationConfig
from brt6.core.behavior_target_cache import (
    freeze_behavior_target_cache,
    validate_behavior_target_cache,
)
from brt6.pipeline.run import (
    build_parser,
    configure_behavior_target_source,
    resume_matches_ablation,
)


class BehaviorTargetCacheTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        instance_id = "demo__repo-1"
        instances = root / "issues.json"
        instances.write_text(
            json.dumps([{"instance_id": instance_id, "problem_statement": "x"}]),
            encoding="utf-8",
        )
        code = root / "code.json"
        tests = root / "tests.json"
        code.write_text('{"same": "code"}\n', encoding="utf-8")
        tests.write_text('{"same": "tests"}\n', encoding="utf-8")
        source = root / "source"
        target = source / instance_id / "behavior_target.json"
        target.parent.mkdir(parents=True)
        target.write_text(
            json.dumps(
                {
                    "schema_version": "behavior_target.lossless.v1",
                    "instance_id": instance_id,
                    "issue_summary": "demo",
                    "setup": {},
                    "trigger": {},
                    "oracle": {},
                    "uncertainties": [],
                    "raw": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        cache = root / "cache"
        freeze_behavior_target_cache(
            source,
            cache,
            instances,
            cache_id="demo-cache-v1",
            dataset_mode="swt",
            code_retrieval_path=code,
            test_retrieval_path=tests,
        )
        return cache, instances, code, tests, target

    def _required_args(
        self, cache: Path, instances: Path, code: Path, tests: Path
    ) -> list[str]:
        return [
            "--instances_path", str(instances),
            "--code_retrieval_path", str(code),
            "--test_retrieval_path", str(tests),
            "--repo_root_base", "repos",
            "--output_dir", "out",
            "--behavior-target-cache", str(cache),
        ]

    def test_valid_cache_is_portable_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, instances, code, tests, _ = self._fixture(Path(tmp))
            provenance = validate_behavior_target_cache(
                cache,
                instances,
                dataset_mode="swt",
                code_retrieval_path=code,
                test_retrieval_path=tests,
            )
            self.assertEqual(provenance["cache_id"], "demo-cache-v1")
            self.assertEqual(provenance["instance_count"], 1)
            self.assertTrue(provenance["source_signature"].startswith("demo-cache-v1:"))

    def test_tampered_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, instances, code, tests, _ = self._fixture(Path(tmp))
            target = cache / "demo__repo-1" / "behavior_target.json"
            target.write_text(target.read_text() + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_behavior_target_cache(
                    cache,
                    instances,
                    dataset_mode="swt",
                    code_retrieval_path=code,
                    test_retrieval_path=tests,
                )

    def test_retrieval_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, instances, code, tests, _ = self._fixture(Path(tmp))
            code.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "code_retrieval_sha256 mismatch"):
                validate_behavior_target_cache(
                    cache,
                    instances,
                    dataset_mode="swt",
                    code_retrieval_path=code,
                    test_retrieval_path=tests,
                )

    def test_explicit_cache_configures_loader_and_resume_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, instances, code, tests, _ = self._fixture(Path(tmp))
            args = build_parser().parse_args(
                self._required_args(cache, instances, code, tests)
            )
            provenance = configure_behavior_target_source(args)
            signature = provenance["source_signature"]
            full = AblationConfig()
            summary = {
                "ablation_signature": full.signature,
                "behavior_target_source_signature": signature,
            }
            self.assertTrue(resume_matches_ablation(summary, full, signature))
            self.assertFalse(
                resume_matches_ablation(summary, full, "different-cache:hash")
            )

    def test_cache_cannot_be_used_in_behavior_target_ablation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, instances, code, tests, _ = self._fixture(Path(tmp))
            args = build_parser().parse_args(
                self._required_args(cache, instances, code, tests)
                + ["--behavior-target", "off"]
            )
            with self.assertRaisesRegex(ValueError, "cannot be combined"):
                configure_behavior_target_source(args)


if __name__ == "__main__":
    unittest.main()
