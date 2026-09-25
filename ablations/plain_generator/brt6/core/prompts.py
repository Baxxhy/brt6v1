"""Prompt compatibility constants loaded from markdown prompt files."""

from __future__ import annotations

from ..prompts.loader import load_prompt


ISSUE_REWRITE_SYSTEM_PROMPT = load_prompt("issue_rewrite", "system")
ISSUE_REWRITE_USER_PROMPT = load_prompt("issue_rewrite", "user")

BEHAVIOR_DISAMBIGUATION_SYSTEM_PROMPT = load_prompt(
    "behavior_disambiguation", "system"
)
BEHAVIOR_DISAMBIGUATION_USER_PROMPT = load_prompt(
    "behavior_disambiguation", "user"
)

HOST_CONTEXT_SYSTEM_PROMPT = load_prompt("behavior_target", "system")
HOST_CONTEXT_USER_PROMPT = load_prompt("behavior_target", "user")

MUTATION_GENERATION_SYSTEM_PROMPT = load_prompt("generation", "system")
MUTATION_GENERATION_USER_PROMPT = load_prompt("generation", "user")
JOINT_SEED_GENERATION_SYSTEM_PROMPT = load_prompt("joint_seed_generation", "system")
JOINT_SEED_GENERATION_USER_PROMPT = load_prompt("joint_seed_generation", "user")

OBSERVATION_PROBE_SYSTEM_PROMPT = load_prompt("observation_probe", "system")
OBSERVATION_PROBE_USER_PROMPT = load_prompt("observation_probe", "user")

ASSERT_SYNTHESIS_SYSTEM_PROMPT = load_prompt("assert_synthesis", "system")
ASSERT_SYNTHESIS_USER_PROMPT = load_prompt("assert_synthesis", "user")

BUGGY_ONLY_VERIFIER_SYSTEM_PROMPT = load_prompt("verifier", "system")
BUGGY_ONLY_VERIFIER_USER_PROMPT = load_prompt("verifier", "user")
JOINT_SEED_VERIFIER_SYSTEM_PROMPT = load_prompt("joint_seed_verifier", "system")
JOINT_SEED_VERIFIER_USER_PROMPT = load_prompt("joint_seed_verifier", "user")

SURROGATE_PATCH_SYSTEM_PROMPT = load_prompt("surrogate_patch", "system")
SURROGATE_PATCH_USER_PROMPT = load_prompt("surrogate_patch", "user")

PROTOCOL_RECOVERY_SYSTEM_PROMPT = load_prompt("protocol_recovery", "system")
PROTOCOL_RECOVERY_USER_PROMPT = load_prompt("protocol_recovery", "user")
JOINT_SEED_PROTOCOL_RECOVERY_SYSTEM_PROMPT = load_prompt(
    "joint_seed_protocol_recovery", "system"
)

SEED_MUTATION_PLAN_SYSTEM_PROMPT = load_prompt("mutation_plan", "system")
SEED_MUTATION_PLAN_USER_PROMPT = load_prompt("mutation_plan", "user")


STRICT_SEMANTIC_VERIFIER_SYSTEM_PROMPT = load_prompt("strict_semantic_verifier", "system")
STRICT_SEMANTIC_VERIFIER_USER_PROMPT = load_prompt("strict_semantic_verifier", "user")
JOINT_SEED_STRICT_VERIFIER_SYSTEM_PROMPT = load_prompt(
    "joint_seed_strict_verifier", "system"
)
