"""Pure decision helpers for the full-run generation/evaluation boundary."""

from __future__ import annotations


def formal_evaluation_allowed(
    generation_returncode: int,
    generated_tests: int,
    dataset_size: int,
    allow_incomplete: bool,
) -> bool:
    if generation_returncode == 75:
        return False
    if dataset_size <= 0 or generated_tests <= 0:
        return False
    if allow_incomplete:
        return True
    return generation_returncode == 0 and generated_tests == dataset_size
