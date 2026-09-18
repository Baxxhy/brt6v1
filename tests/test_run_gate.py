from brt6.core.run_gate import formal_evaluation_allowed


def test_api_pause_never_automatically_enters_official_evaluation():
    assert not formal_evaluation_allowed(75, 276, 276, True)
    assert not formal_evaluation_allowed(75, 262, 276, False)


def test_complete_generation_requires_successful_generation() -> None:
    assert formal_evaluation_allowed(0, 276, 276, False)
    assert not formal_evaluation_allowed(1, 276, 276, False)


def test_incomplete_evaluation_ignores_generation_returncode_when_tests_exist() -> None:
    assert formal_evaluation_allowed(1, 262, 276, True)
    assert not formal_evaluation_allowed(1, 0, 276, True)
