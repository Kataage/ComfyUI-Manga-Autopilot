from __future__ import annotations

from manga_autopilot.models.generation_profile import SemanticPromptSegments
from manga_autopilot.services.anima_prompt_builder import AnimaPromptBuilder
from manga_autopilot.services.generation_profiles import load_builtin_profile


def test_anima_prompt_order_deduplicates_and_keeps_identity_first() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(
        must_keep=["black bob hair", "amber eyes", "black bob hair"],
        subject=["1girl", "amber eyes"],
        action=["holding umbrella"],
        camera=["low angle"],
        emotion=["determined"],
        background=["old shrine"],
        lighting=["rainy evening"],
        style=["clean lineart"],
        negative=["extra arms"],
    )

    result = AnimaPromptBuilder().render(
        segments,
        profile,
        seed=123,
        panel_size=(3, 4),
    )

    assert result.positive.index("black bob hair") < result.positive.index("1girl")
    assert result.positive.count("black bob hair") == 1
    assert result.positive.count("amber eyes") == 1
    assert result.positive.index("holding umbrella") < result.positive.index("low angle")
    assert "text" in result.negative_full()
    assert "speech text in image" in result.negative_full()


def test_profile_owns_all_technical_generation_fields() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(
        subject=["1girl"],
        technical_overrides={
            "steps": 99,
            "cfg": 20,
            "seed": 999,
            "width": 64,
            "height": 64,
        },
    )

    result = AnimaPromptBuilder().render(
        segments,
        profile,
        seed=42,
        panel_size=(4, 3),
    )

    assert (result.steps, result.cfg) == (12, 1)
    assert result.seed == 42
    assert (result.width, result.height) == (1280, 960)
    assert (result.sampler, result.scheduler) == ("er_sde", "simple")


def test_aesthetic_profile_removes_score_tags() -> None:
    profile = load_builtin_profile("anima_aesthetic")
    segments = SemanticPromptSegments(
        subject=["1girl"],
        style=["score_7", "watercolor"],
        negative=["score_1"],
    )

    result = AnimaPromptBuilder().render(segments, profile, seed=1)

    assert "score_" not in result.positive
    assert "score_" not in result.negative


def test_identity_precedes_must_keep_and_full_segment_order_is_stable() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(
        identity=["hero_a"],
        must_keep=["red scarf"],
        subject=["1boy"],
        action=["running"],
        camera=["dutch angle"],
        emotion=["panicked"],
        background=["narrow alley"],
        lighting=["harsh noon sun"],
        style=["screentone shading"],
    )

    positive = AnimaPromptBuilder().render(segments, profile, seed=7).positive
    order = [
        positive.index(term)
        for term in (
            "hero_a",
            "red scarf",
            "1boy",
            "running",
            "dutch angle",
            "panicked",
            "narrow alley",
            "harsh noon sun",
            "screentone shading",
        )
    ]

    assert order == sorted(order)
    assert positive.startswith("masterpiece, best quality")


def test_comma_packed_segments_are_split_and_deduplicated_case_insensitively() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(
        must_keep=["black bob hair, amber eyes"],
        subject=["Amber Eyes", "1girl", "  1girl  "],
    )

    positive = AnimaPromptBuilder().render(segments, profile, seed=7).positive

    assert positive.count("1girl") == 1
    assert "Amber Eyes" not in positive
    assert positive.index("amber eyes") < positive.index("1girl")


def test_base_profile_keeps_score_tags() -> None:
    profile = load_builtin_profile("anima_base")
    segments = SemanticPromptSegments(subject=["1girl"], style=["score_7"])

    result = AnimaPromptBuilder().render(segments, profile, seed=3)

    assert "score_7" in result.positive


def test_empty_segments_still_render_profile_defaults() -> None:
    profile = load_builtin_profile("anima_turbo")

    result = AnimaPromptBuilder().render(SemanticPromptSegments(), profile, seed=5)

    assert result.positive == "masterpiece, best quality, score_7, safe, anime, manga, clean lineart"
    assert result.negative == "lowres, jpeg artifacts, signature, username"
    assert result.character_prompt == ""


def test_application_bans_survive_profile_and_segment_negatives() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(negative=["extra arms", "lowres"])

    negative_full = AnimaPromptBuilder().render(segments, profile, seed=5).negative_full()

    for ban in ("text", "watermark", "speech text in image", "different hair color"):
        assert ban in negative_full
    assert "extra arms" in negative_full


def test_default_panel_size_renders_the_portrait_resolution() -> None:
    profile = load_builtin_profile("anima_turbo")

    result = AnimaPromptBuilder().render(SemanticPromptSegments(), profile, seed=5)

    assert (result.width, result.height) == (960, 1280)


def test_sampler_and_scheduler_overrides_are_ignored() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(
        subject=["1girl"],
        technical_overrides={"sampler": "euler", "scheduler": "karras"},
    )

    result = AnimaPromptBuilder().render(segments, profile, seed=5)

    assert (result.sampler, result.scheduler) == ("er_sde", "simple")


def test_rendering_is_deterministic_for_the_same_inputs() -> None:
    profile = load_builtin_profile("anima_turbo")
    segments = SemanticPromptSegments(must_keep=["silver hair"], subject=["1girl"])

    first = AnimaPromptBuilder().render(segments, profile, seed=11, panel_size=(1, 1))
    second = AnimaPromptBuilder().render(segments, profile, seed=11, panel_size=(1, 1))

    assert first.model_dump() == second.model_dump()
