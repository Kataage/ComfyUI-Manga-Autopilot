from __future__ import annotations

import pytest

from manga_autopilot.services.generation_profiles import (
    list_builtin_profiles,
    load_builtin_profile,
    resolve_default_resolution,
    resolve_panel_resolution,
)


def test_builtin_anima_profiles_load_with_license_metadata() -> None:
    profiles = [
        load_builtin_profile(profile_id)
        for profile_id in ("anima_base", "anima_aesthetic", "anima_turbo")
    ]

    assert [profile.id for profile in profiles] == [
        "anima_base",
        "anima_aesthetic",
        "anima_turbo",
    ]
    assert all(profile.license.requires_acknowledgement for profile in profiles)
    assert all(profile.license.url.startswith("https://huggingface.co/") for profile in profiles)


def test_turbo_profile_matches_verified_local_workflow() -> None:
    profile = load_builtin_profile("anima_turbo")

    assert profile.generation.steps == 12
    assert profile.generation.cfg == 1
    assert profile.generation.sampler == "er_sde"
    assert profile.generation.scheduler == "simple"
    assert profile.candidate_count == 1
    assert profile.technical_retry_count == 1
    assert profile.quality_retry_count == 0


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((3, 4), (960, 1280)),
        ((4, 3), (1280, 960)),
        ((1, 1), (1088, 1088)),
    ],
)
def test_turbo_resolution_policy(size: tuple[int, int], expected: tuple[int, int]) -> None:
    profile = load_builtin_profile("anima_turbo")

    result = resolve_panel_resolution(*size, profile.resolution)

    assert result.size == expected
    assert result.width % 64 == 0
    assert result.height % 64 == 0
    assert 512 <= result.width <= 1536
    assert 512 <= result.height <= 1536


def test_resolution_rejects_non_positive_panel_size() -> None:
    with pytest.raises(ValueError, match="positive"):
        resolve_panel_resolution(0, 4, load_builtin_profile("anima_turbo").resolution)


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(KeyError, match="unknown generation profile"):
        load_builtin_profile("anima_missing")


def test_extreme_aspect_ratio_is_clamped_to_supported_sides() -> None:
    policy = load_builtin_profile("anima_turbo").resolution

    wide = resolve_panel_resolution(8, 1, policy)
    tall = resolve_panel_resolution(1, 8, policy)

    assert wide.size == (policy.max_side, policy.min_side)
    assert tall.size == (policy.min_side, policy.max_side)
    for result in (wide, tall):
        assert result.width % policy.multiple_of == 0
        assert result.height % policy.multiple_of == 0


def test_resolution_depends_only_on_the_aspect_ratio() -> None:
    policy = load_builtin_profile("anima_turbo").resolution

    assert resolve_panel_resolution(3, 4, policy).size == resolve_panel_resolution(
        300.0, 400.0, policy
    ).size


def test_moderate_aspect_ratios_stay_near_the_pixel_budget() -> None:
    policy = load_builtin_profile("anima_turbo").resolution

    for width, height in ((3, 4), (4, 3), (1, 1), (2, 3), (16, 9)):
        result = resolve_panel_resolution(width, height, policy)
        pixels = result.width * result.height
        assert 0.85 <= pixels / policy.target_pixels <= 1.15


def test_every_builtin_profile_resolves_a_valid_default_resolution() -> None:
    for profile in list_builtin_profiles():
        result = resolve_default_resolution(profile.resolution)
        assert result.size == (960, 1280)
        assert profile.resolution.min_side <= result.width <= profile.resolution.max_side


def test_builtin_profiles_forbid_bundling_and_auto_download() -> None:
    for profile in list_builtin_profiles():
        assert profile.license.allows_bundling is False
        assert profile.license.allows_auto_download is False


def test_aesthetic_profile_disables_score_tags_while_base_and_turbo_keep_them() -> None:
    assert load_builtin_profile("anima_aesthetic").allow_score_tags is False
    assert load_builtin_profile("anima_base").allow_score_tags is True
    assert load_builtin_profile("anima_turbo").allow_score_tags is True


def test_base_profile_follows_model_card_step_and_cfg_guidance() -> None:
    profile = load_builtin_profile("anima_base")

    assert 30 <= profile.generation.steps <= 50
    assert 4 <= profile.generation.cfg <= 5
    assert profile.assets.loras == []


def test_turbo_profile_declares_the_verified_local_assets() -> None:
    profile = load_builtin_profile("anima_turbo")

    assert profile.assets.unet == "silvermoonmixAnima_v23.safetensors"
    assert profile.assets.text_encoder == "qwen_3_06b_base.safetensors"
    assert profile.assets.vae == "qwen_image_vae.safetensors"
    assert [lora.name for lora in profile.assets.loras] == [
        "anima-turbo-lora-v0.2.safetensors"
    ]
