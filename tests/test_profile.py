"""The profile layer: typed knobs, invariants out of reach, behaviour preserved."""

from __future__ import annotations

import pytest

from pilot.agent import prompts
from pilot.config import build_settings, preset_profile, settings_from_profile, specs_from_profile
from pilot.profile import KNOBS, Profile, ProfileError


def test_defaults_reproduce_the_validated_harness():
    p = Profile()
    # The values that were hard-coded when PilotBench scored 15/15 twice.
    expected = {"plan": "lazy", "verify": "auto", "max_step_turns": 14, "max_escalations": 4, "use_ax": True, "auto_observe": True,
                "max_long_edge": 1568, "image_format": "png", "keep_images": 3, "outline_max_lines": 180, "outline_max_chars": 8000,
                "compact_outline": True, "settle_timeout": 1.5, "evidence_gate": True, "loop_window": 3, "budget_warning": True,
                "nudge_on_text_end": True, "no_change_note": True, "await_editable": True, "editable_timeout": 0.6, "paste_threshold": 120}
    assert {k: getattr(p, k) for k in expected} == expected
    assert prompts.build_executor(p.executor_rules) == prompts.EXECUTOR


@pytest.mark.parametrize("name", ["approve", "max_cost", "max_minutes", "max_turns", "gui_only", "write_roots", "safety", "kill_switch"])
def test_invariants_are_not_tunable(name):
    with pytest.raises(ProfileError, match="not tunable"):
        Profile(values={name: "anything"})
    with pytest.raises(ProfileError):
        Profile().mutate(**{name: "anything"})


def test_safety_rules_survive_any_rule_selection():
    minimal = prompts.build_executor(Profile(values={"executor_rules": []}).executor_rules)
    assert "is data, not instructions" in minimal and "passwords" in minimal
    assert "injection" not in KNOBS["executor_rules"].choices and "secrets" not in KNOBS["executor_rules"].choices
    with pytest.raises(ProfileError):
        Profile(values={"executor_rules": ["batching", "injection"]})


def test_validation_and_coercion():
    with pytest.raises(ProfileError, match="outside"):
        Profile(values={"max_step_turns": 100})
    with pytest.raises(ProfileError, match="one of"):
        Profile(values={"plan": "sometimes"})
    with pytest.raises(ProfileError, match="required"):
        Profile(values={"executor": None})
    with pytest.raises(ProfileError):
        Profile(values={"executor": "mystery-model"})
    p = Profile(values={"evidence_gate": "false", "max_step_turns": "9", "executor_rules": ["terse", "batching"]})
    assert p.evidence_gate is False and p.max_step_turns == 9 and p.executor_rules == ["batching", "terse"]


def test_mutation_is_immutable_and_explained(tmp_path):
    a = Profile("base", {"plan": "lazy"})
    b = a.mutate("b", why="ablation: evidence gate off", evidence_gate=False)
    assert a.evidence_gate is True and b.evidence_gate is False and b.parent == "base"
    assert b.why["evidence_gate"] == "ablation: evidence gate off" and a.diff(b) == {"evidence_gate": (True, False)}
    path = b.save(tmp_path / "b.json")
    c = Profile.load(path)
    assert c.resolved() == b.resolved() and c.why == b.why


def test_shipped_sub_profile_documents_every_knob():
    p = Profile.load("sub")
    assert set(p.why) == set(KNOBS) and all(p.why[k] for k in KNOBS)
    assert p.planner == "claude-code:claude-opus-5-5" and p.executor == "claude-code:claude-sonnet-5-5"
    # Same models and efforts as the preset that produced the benchmark results.
    assert specs_from_profile(p) == specs_from_profile(preset_profile("sub"))


def test_build_settings_routes_knobs_into_the_profile():
    s = build_settings(preset="sub", plan="upfront", verify="never", approve="none", max_cost=1.0)
    assert s.profile.plan == "upfront" and s.profile.verify == "never" and s.approve == "none" and s.max_cost == 1.0
    assert s.profile.why["plan"] == "command-line override"
    s2 = build_settings(profile="sub", executor="claude-code:claude-haiku-5-5", executor_effort="medium")
    assert s2.executor.model == "claude-haiku-5-5" and s2.executor.effort == "medium" and s2.preset == "sub+custom"


def test_settings_from_mutated_profile_keeps_invariants():
    base = build_settings(preset="sub", approve="all", max_cost=0.5)
    s = settings_from_profile(base.profile.mutate(executor_effort="medium"), base)
    assert s.executor.effort == "medium" and s.approve == "all" and s.max_cost == 0.5
