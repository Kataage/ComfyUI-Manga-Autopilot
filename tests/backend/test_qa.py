"""Tests for the QA + retry + candidate generation module (spec 17-18)."""

from __future__ import annotations

import pytest
from PIL import Image

from manga_autopilot.models.character import (
    Character,
    CharacterAppearance,
    ColorPalette,
    Outfit,
)
from manga_autopilot.models.page import PanelPlan
from manga_autopilot.services.prompt_builder import PromptSpec
from manga_autopilot.services.qa import (
    SEED_POLICIES,
    BubbleSpaceChecker,
    CandidateGenerator,
    CandidateImage,
    CandidateSpec,
    CharacterConsistencyChecker,
    CheckName,
    FallbackGenerator,
    PanelGeometry,
    PromptAlignmentChecker,
    PromptRevisionRules,
    QualityCheck,
    QualityResult,
    RetryAction,
    RetryController,
    quality_result_for,
)


def _panel(**kwargs) -> PanelPlan:
    defaults = dict(
        panel_number=1,
        purpose="impact",
        shot="close-up",
        action="attack",
        emotion="angry",
        background="ruins",
    )
    defaults.update(kwargs)
    return PanelPlan(**defaults)


def _geom(**kwargs) -> PanelGeometry:
    defaults = dict(panel_id="p1", x=0, y=0, width=256, height=256)
    defaults.update(kwargs)
    return PanelGeometry(**defaults)


def _prompt() -> PromptSpec:
    return PromptSpec(
        positive="silver long hair, blue eyes, attack, ruins, angry, close-up",
        negative="",
    )


def _candidate(seed: int = 1, w: int = 256, h: int = 256) -> CandidateImage:
    return CandidateImage(
        candidate_id="p1_c00",
        panel_id="p1",
        seed=seed,
        prompt=_prompt(),
        image=Image.new("RGB", (w, h), (128, 64, 32)),
        width=w,
        height=h,
    )


def test_candidate_generator_respects_max() -> None:
    gen = CandidateGenerator(max_candidates=3)
    spec = CandidateSpec(panel_id="p1", candidate_count=10, base_seed=100)
    out = gen.generate(spec)
    assert len(out) == 3
    assert out[0].seed == 100
    assert out[1].seed == 101


def test_seed_policy_fixed() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(panel_id="p1", candidate_count=3, base_seed=42, seed_policy="fixed")
    seeds = [c.seed for c in gen.generate(spec)]
    assert seeds == [42, 42, 42]


def test_seed_policy_panel_random() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(panel_id="p1", candidate_count=4, base_seed=42, seed_policy="panel_random")
    seeds = [c.seed for c in gen.generate(spec)]
    assert len(set(seeds)) > 1


def test_seed_policy_character_fixed() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(
        panel_id="p1",
        candidate_count=3,
        seed_policy="character_fixed_panel_random",
    )
    seeds = [c.seed for c in gen.generate(spec, character_seeds=[777])]
    assert seeds == [777, 777, 777]


def test_seed_policy_base_seed_plus_index() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(panel_id="p1", candidate_count=3, base_seed=10, seed_policy="base_seed_plus_index")
    seeds = [c.seed for c in gen.generate(spec)]
    assert seeds == [10, 11, 12]


def test_unknown_seed_policy() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(panel_id="p1", candidate_count=1, seed_policy="weird")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        gen.generate(spec)


def test_candidate_count_zero_rejected() -> None:
    gen = CandidateGenerator()
    spec = CandidateSpec(panel_id="p1", candidate_count=0)
    with pytest.raises(ValueError):
        gen.generate(spec)


def test_quality_check_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        QualityCheck(CheckName.FACE_QUALITY, score=1.5)
    with pytest.raises(ValueError):
        QualityCheck(CheckName.FACE_QUALITY, score=0.5, weight=-0.1)


def test_quality_result_weighted_total() -> None:
    result = QualityResult(panel_id="p", candidate_id="c")
    result.add_check(QualityCheck(CheckName.CHARACTER_CONSISTENCY, 0.5, weight=0.30))
    result.add_check(QualityCheck(CheckName.PROMPT_ALIGNMENT, 0.8, weight=0.20))
    result.add_check(QualityCheck(CheckName.FACE_QUALITY, 0.6, weight=0.15))
    result.add_check(QualityCheck(CheckName.BUBBLE_SPACE, 1.0, weight=0.05))
    # weighted average
    expected = (0.5 * 0.30 + 0.8 * 0.20 + 0.6 * 0.15 + 1.0 * 0.05) / 0.70
    assert result.total() == pytest.approx(expected, rel=1e-6)


def test_quality_result_finalize_passes() -> None:
    result = QualityResult(panel_id="p", candidate_id="c", threshold=0.5)
    result.add_check(QualityCheck(CheckName.CHARACTER_CONSISTENCY, 0.9, weight=1.0))
    score = result.finalize()
    assert score == pytest.approx(0.9)
    assert result.passed is True


def test_bubble_space_checker_passes_with_margin() -> None:
    geom = _geom(x=16, y=16, width=200, height=200)
    cand = _candidate(w=256, h=256)
    check = BubbleSpaceChecker(min_margin=16).check(geom, cand)
    assert check.score == 1.0


def test_bubble_space_checker_fails_without_margin() -> None:
    geom = _geom(x=0, y=0, width=256, height=256)
    cand = _candidate(w=256, h=256)
    check = BubbleSpaceChecker(min_margin=16).check(geom, cand)
    assert check.score == 0.0


def test_prompt_alignment_checker() -> None:
    panel = _panel(action="attack", background="ruins", shot="close-up", emotion="angry")
    prompt = _prompt()
    check = PromptAlignmentChecker().check(panel, prompt)
    assert check.score == pytest.approx(1.0)


def test_prompt_alignment_partial() -> None:
    panel = _panel(action="attack", background="forest", shot="close-up", emotion="angry")
    prompt = _prompt()
    check = PromptAlignmentChecker().check(panel, prompt)
    assert 0.0 < check.score < 1.0


def test_character_consistency_checker_no_reference() -> None:
    check = CharacterConsistencyChecker().check(None, _prompt())
    assert check.score == 1.0


def test_character_consistency_checker_mustkeep() -> None:
    char = Character(
        id="a",
        name="A",
        appearance=CharacterAppearance(hair_color="silver", hair_style="long", eye_color="blue"),
        outfit=Outfit(must_keep=["silver long hair", "blue eyes"]),
        color_palette=ColorPalette(primary="#000000"),
    )
    check = CharacterConsistencyChecker().check(char, _prompt())
    assert check.score == pytest.approx(1.0)


def test_retry_rules_revise_prompt() -> None:
    rules = PromptRevisionRules()
    issues = [dict(category="face_quality", message="face collapsed")]
    new_prompt, actions = rules.revise(_prompt(), issues)
    assert "face close-up" in new_prompt.positive
    assert RetryAction.REVISE_PROMPT in actions


def test_retry_rules_text_artifact() -> None:
    rules = PromptRevisionRules()
    issues = [dict(category="text_artifact", message="has letters")]
    new_prompt, _ = rules.revise(_prompt(), issues)
    assert "text" in new_prompt.negative


def test_retry_rules_hand_quality_simplifies() -> None:
    rules = PromptRevisionRules()
    issues = [dict(category="hand_quality", message="hand broken")]
    new_prompt, actions = rules.revise(_prompt(), issues)
    assert "upper body shot" in new_prompt.positive
    assert RetryAction.SIMPLIFY_COMPOSITION in actions


def test_retry_controller_uses_fallback_after_max() -> None:
    ctrl = RetryController(max_retry=2)
    assert ctrl.next_action(2, [{"category": "face_quality"}]) == RetryAction.USE_FALLBACK


def test_retry_controller_revise_returns_decision() -> None:
    ctrl = RetryController()
    revised, decision = ctrl.revise(
        _prompt(),
        [dict(category="character_consistency", message="wrong color")],
    )
    assert decision.action == RetryAction.REVISE_PROMPT
    assert "silver long hair" in revised.positive


def test_fallback_generator_produces_safe_image() -> None:
    panel = _panel()
    img = FallbackGenerator(width=128, height=128).generate(panel)
    assert img.candidate_id.endswith("fallback")
    assert img.width == 128
    assert img.image.size == (128, 128)


def test_quality_result_for_aggregates() -> None:
    panel = _panel()
    cand = _candidate()
    geom = _geom(x=16, y=16, width=200, height=200)
    result = quality_result_for(panel, cand, geom, threshold=0.4)
    assert CheckName.BUBBLE_SPACE in result.checks
    assert CheckName.PROMPT_ALIGNMENT in result.checks
    assert CheckName.CHARACTER_CONSISTENCY in result.checks
    assert 0.0 <= result.total() <= 1.0


def test_seeding_policies_typed_constant() -> None:
    assert "base_seed_plus_index" in SEED_POLICIES
    assert len(SEED_POLICIES) == 4
