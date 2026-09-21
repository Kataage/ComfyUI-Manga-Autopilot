from __future__ import annotations

from pathlib import Path

import pytest

from manga_autopilot.models.generation_profile import ResolutionPolicy
from manga_autopilot.models.workflow import WorkflowDefinition
from manga_autopilot.services.generation_profiles import load_builtin_profile
from manga_autopilot.services.preflight import (
    CAPABILITIES_UNAVAILABLE,
    AnimaPreflight,
    ComfyCapabilities,
    PreflightError,
    PreflightRequest,
)

# Shape confirmed against a live ComfyUI 0.30.0 /object_info on 2026-08-26:
# combo inputs are ``[[option, ...], {config}]`` and typed inputs are ``["MODEL"]``.
OBJECT_INFO = {
    "UNETLoader": {
        "input": {
            "required": {
                "unet_name": [["silvermoonmixAnima_v23.safetensors", "other.safetensors"], {}],
                "weight_dtype": [["default", "fp8_e4m3fn"], {}],
            }
        }
    },
    "CLIPLoader": {
        "input": {"required": {"clip_name": [["qwen_3_06b_base.safetensors"], {}]}}
    },
    "VAELoader": {"input": {"required": {"vae_name": [["qwen_image_vae.safetensors"], {}]}}},
    "LoraLoader": {
        "input": {
            "required": {
                "model": ["MODEL"],
                "lora_name": [["anima-turbo-lora-v0.2.safetensors"], {}],
                "strength_model": ["FLOAT"],
            }
        }
    },
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING"], "clip": ["CLIP"]}}},
    "EmptyLatentImage": {
        "input": {"required": {"width": ["INT"], "height": ["INT"], "batch_size": ["INT"]}}
    },
    "KSampler": {
        "input": {
            "required": {
                "seed": ["INT"],
                "steps": ["INT"],
                "cfg": ["FLOAT"],
                "sampler_name": [["er_sde", "euler"], {}],
                "scheduler": [["simple", "normal"], {}],
            }
        }
    },
    "SaveImage": {"input": {"required": {"filename_prefix": ["STRING"], "images": ["IMAGE"]}}},
}

API_GRAPH = {
    "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "silvermoonmixAnima_v23.safetensors"}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["3", 0]}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["3", 0]}},
    "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 960, "height": 1280, "batch_size": 1}},
    "5": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 0,
            "steps": 12,
            "cfg": 1.0,
            "sampler_name": "er_sde",
            "scheduler": "simple",
        },
    },
    "6": {"class_type": "SaveImage", "inputs": {"filename_prefix": "panel"}},
}

BINDINGS = {
    "positive_prompt": {"node_id": "2", "input": "text"},
    "negative_prompt": {"node_id": "3", "input": "text"},
    "seed": {"node_id": "5", "input": "seed"},
    "steps": {"node_id": "5", "input": "steps"},
    "cfg": {"node_id": "5", "input": "cfg"},
    "width": {"node_id": "4", "input": "width"},
    "height": {"node_id": "4", "input": "height"},
    "filename_prefix": {"node_id": "6", "input": "filename_prefix"},
}


def _workflow(**overrides) -> WorkflowDefinition:
    payload = {
        "workflow_id": "anima_turbo",
        "name": "Anima Turbo",
        "type": "text_to_image",
        "file": "anima_turbo.workflow.json",
        "bindings": dict(BINDINGS),
        "api_graph": dict(API_GRAPH),
    }
    payload.update(overrides)
    return WorkflowDefinition.model_validate(payload)


def _request(tmp_path: Path, **overrides) -> PreflightRequest:
    defaults = {
        "profile": load_builtin_profile("anima_turbo"),
        "workflow": _workflow(),
        "output_dir": tmp_path / "out",
        "comfy_base_url": "http://127.0.0.1:8188",
        "license_acknowledged": True,
        "panel_sizes": [(3, 4), (4, 3)],
    }
    defaults.update(overrides)
    return PreflightRequest(**defaults)


def _preflight() -> AnimaPreflight:
    return AnimaPreflight(ComfyCapabilities.from_object_info(OBJECT_INFO))


# ------------------------------------------------------------- capabilities


def test_capabilities_read_combo_options_and_ignore_typed_inputs() -> None:
    capabilities = ComfyCapabilities.from_object_info(OBJECT_INFO)

    assert "UNETLoader" in capabilities.node_classes
    assert "silvermoonmixAnima_v23.safetensors" in capabilities.files["unet_name"]
    assert "anima-turbo-lora-v0.2.safetensors" in capabilities.files["lora_name"]
    assert "MODEL" not in capabilities.files.get("model", frozenset())


# -------------------------------------------------------------- happy path


def test_local_anima_setup_passes_preflight(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path))

    assert report.ok is True
    assert report.errors == ()
    report.raise_if_blocked()


# --------------------------------------------------------------- endpoints


def test_loopback_endpoint_needs_no_auth(tmp_path: Path) -> None:
    for url in ("http://127.0.0.1:8188", "http://localhost:8188", "http://[::1]:8188"):
        report = _preflight().run(_request(tmp_path, comfy_base_url=url))
        assert "comfy.remote_not_allowed" not in report.codes()
        assert "comfy.remote_without_auth" not in report.codes()


def test_remote_endpoint_is_blocked_unless_explicitly_allowed(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, comfy_base_url="http://192.168.1.50:8188"))

    assert report.ok is False
    assert "comfy.remote_not_allowed" in report.codes()


def test_allowed_remote_endpoint_still_requires_auth(tmp_path: Path) -> None:
    report = _preflight().run(
        _request(
            tmp_path,
            comfy_base_url="http://192.168.1.50:8188",
            allow_remote_comfyui=True,
        )
    )

    assert "comfy.remote_without_auth" in report.codes()

    with_auth = _preflight().run(
        _request(
            tmp_path,
            comfy_base_url="http://192.168.1.50:8188",
            allow_remote_comfyui=True,
            comfy_auth_configured=True,
        )
    )
    assert with_auth.ok is True


# ---------------------------------------------------------------- workflow


def test_binding_pointing_at_a_missing_node_is_blocking(tmp_path: Path) -> None:
    bindings = dict(BINDINGS, seed={"node_id": "999", "input": "seed"})

    report = _preflight().run(_request(tmp_path, workflow=_workflow(bindings=bindings)))

    assert report.ok is False
    assert "workflow.node_missing" in report.codes()
    assert any("999" in issue.message for issue in report.errors)


def test_binding_pointing_at_a_missing_input_is_blocking(tmp_path: Path) -> None:
    bindings = dict(BINDINGS, seed={"node_id": "5", "input": "noise_seed"})

    report = _preflight().run(_request(tmp_path, workflow=_workflow(bindings=bindings)))

    assert "workflow.input_missing" in report.codes()


def test_workflow_using_an_unavailable_node_class_is_blocking(tmp_path: Path) -> None:
    graph = dict(API_GRAPH)
    graph["7"] = {"class_type": "SomeMissingCustomNode", "inputs": {}}

    report = _preflight().run(_request(tmp_path, workflow=_workflow(api_graph=graph)))

    assert "workflow.node_class_unavailable" in report.codes()
    assert any("SomeMissingCustomNode" in issue.message for issue in report.errors)


def test_workflow_without_an_api_graph_is_blocking(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, workflow=_workflow(api_graph=None)))

    assert "workflow.api_graph_absent" in report.codes()


def test_unbound_technical_fields_are_warned_not_blocked(tmp_path: Path) -> None:
    bindings = {k: v for k, v in BINDINGS.items() if k not in {"steps", "cfg"}}

    report = _preflight().run(_request(tmp_path, workflow=_workflow(bindings=bindings)))

    assert report.ok is True
    assert "workflow.technical_field_unbound" in report.codes()
    assert {issue.severity for issue in report.warnings} == {"warning"}


# ------------------------------------------------------------------ models


def test_missing_model_file_is_blocking(tmp_path: Path) -> None:
    info = {
        **OBJECT_INFO,
        "UNETLoader": {"input": {"required": {"unet_name": [["other.safetensors"], {}]}}},
    }
    preflight = AnimaPreflight(ComfyCapabilities.from_object_info(info))

    report = preflight.run(_request(tmp_path))

    assert report.ok is False
    assert "model.missing" in report.codes()
    assert any("silvermoonmixAnima_v23.safetensors" in i.message for i in report.errors)


def test_missing_lora_is_blocking(tmp_path: Path) -> None:
    info = {
        **OBJECT_INFO,
        "LoraLoader": {"input": {"required": {"lora_name": [["something_else.safetensors"], {}]}}},
    }
    preflight = AnimaPreflight(ComfyCapabilities.from_object_info(info))

    report = preflight.run(_request(tmp_path))

    assert "lora.missing" in report.codes()
    assert any("anima-turbo-lora-v0.2.safetensors" in i.message for i in report.errors)


def test_profile_without_loras_does_not_require_any(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, profile=load_builtin_profile("anima_base")))

    assert "lora.missing" not in report.codes()


# -------------------------------------------------------------- references


def test_missing_character_reference_is_blocking(tmp_path: Path) -> None:
    report = _preflight().run(
        _request(tmp_path, required_reference_character_ids=["hero_a", "hero_b"])
    )

    assert "reference.missing" in report.codes()
    assert any("hero_a" in i.message for i in report.errors)


def test_reference_pointing_at_an_absent_file_is_blocking(tmp_path: Path) -> None:
    report = _preflight().run(
        _request(
            tmp_path,
            required_reference_character_ids=["hero_a"],
            available_reference_images={"hero_a": tmp_path / "gone.png"},
        )
    )

    assert "reference.missing" in report.codes()


def test_present_reference_passes(tmp_path: Path) -> None:
    reference = tmp_path / "hero.png"
    reference.write_bytes(b"png")

    report = _preflight().run(
        _request(
            tmp_path,
            required_reference_character_ids=["hero_a"],
            available_reference_images={"hero_a": reference},
        )
    )

    assert "reference.missing" not in report.codes()


# -------------------------------------------------------------- resolution


def test_self_inconsistent_resolution_policy_is_blocking(tmp_path: Path) -> None:
    profile = load_builtin_profile("anima_turbo").model_copy(
        update={"resolution": ResolutionPolicy(min_side=1000, max_side=1536, multiple_of=64)}
    )

    report = _preflight().run(_request(tmp_path, profile=profile))

    assert "resolution.policy_invalid" in report.codes()


def test_clamped_panel_aspect_is_warned(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, panel_sizes=[(8, 1)]))

    assert report.ok is True
    assert "resolution.aspect_clamped" in report.codes()


def test_normal_panel_aspects_are_not_warned(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, panel_sizes=[(3, 4), (1, 1), (16, 9)]))

    assert "resolution.aspect_clamped" not in report.codes()


# ------------------------------------------------------------------ output


def test_unwritable_output_directory_is_blocking(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")

    report = _preflight().run(_request(tmp_path, output_dir=blocker / "out"))

    assert report.ok is False
    assert "output.unwritable" in report.codes()


def test_preflight_creates_no_files_in_a_writable_output_dir(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()

    _preflight().run(_request(tmp_path, output_dir=out))

    assert list(out.iterdir()) == []


# ----------------------------------------------------------------- licence


def test_missing_license_acknowledgement_is_blocking(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path, license_acknowledged=False))

    assert report.ok is False
    assert "license.not_acknowledged" in report.codes()
    assert any("CircleStone" in i.message for i in report.errors)


# ------------------------------------------------------------------ raising


def test_raise_if_blocked_reports_every_error(tmp_path: Path) -> None:
    report = _preflight().run(
        _request(
            tmp_path,
            license_acknowledged=False,
            comfy_base_url="http://192.168.1.50:8188",
        )
    )

    with pytest.raises(PreflightError) as excinfo:
        report.raise_if_blocked()

    message = str(excinfo.value)
    assert "license.not_acknowledged" in message
    assert "comfy.remote_not_allowed" in message
    assert excinfo.value.report is report


# ------------------------------------------------------- negative at CFG 1


def test_cfg1_profiles_warn_that_the_negative_prompt_is_inert(tmp_path: Path) -> None:
    """anima_turbo renders at CFG 1, where ComfyUI skips the negative branch.

    Verified against comfy/samplers.py (ComfyUI 0.30.0): ``sampling_function``
    sets ``uncond_ = None`` when ``cond_scale`` is close to 1.0.
    """
    report = _preflight().run(_request(tmp_path))

    assert report.ok is True
    assert "prompt.negative_inert_at_cfg1" in report.codes()
    assert any("no effect" in issue.message for issue in report.warnings)


def test_higher_cfg_profiles_are_not_warned(tmp_path: Path) -> None:
    for profile_id in ("anima_base", "anima_aesthetic"):
        report = _preflight().run(
            _request(tmp_path, profile=load_builtin_profile(profile_id))
        )
        assert "prompt.negative_inert_at_cfg1" not in report.codes()


# ------------------------------------------------ a stopped server, named as such


async def test_an_unreachable_comfyui_is_named_in_the_error(tmp_path: Path) -> None:
    """A live run failed as FAILED_PANEL_GENERATION when ComfyUI was simply down."""
    from aiohttp import web

    from manga_autopilot.routes.autopilot_routes import _run_anima_preflight
    from manga_autopilot.services.autopilot import AutopilotRun, AutopilotStateMachine

    class _DeadClient:
        base_url = "http://127.0.0.1:8188"

        async def get_object_info(self):
            raise ConnectionRefusedError("connection refused")

    class _Registry:
        def get(self, workflow_id):
            return _workflow()

    app = web.Application()
    app["manga_comfy_client"] = _DeadClient()
    app["manga_workflow_registry"] = _Registry()
    run = AutopilotRun(
        project_id="p",
        machine=AutopilotStateMachine(project_id="p"),
        input={"generation_profile_id": "anima_turbo"},
    )

    with pytest.raises(RuntimeError, match="not reachable"):
        await _run_anima_preflight(
            app, run, project_root=tmp_path, workflow_id="anima_turbo"
        )


# ------------------------------------------- running without a live ComfyUI


def test_without_capabilities_the_other_six_checks_still_run(tmp_path: Path) -> None:
    """A missing `/object_info` must not take the whole preflight down with it.

    ``manga_remote_executor`` is a supported deployment with no local ComfyUI to
    interrogate. Stepping aside entirely used to drop the licence and endpoint
    checks too, which have nothing to do with `/object_info`.
    """

    report = AnimaPreflight(None).run(_request(tmp_path, workflow=None))

    assert CAPABILITIES_UNAVAILABLE in report.codes()
    assert report.ok, f"nothing should block a well-formed request: {report.issues}"
    # The two capability checks are the only ones held back.
    assert not any(code.startswith("model.") for code in report.codes())
    assert not any(code.startswith("workflow.") for code in report.codes())


def test_without_capabilities_the_licence_gate_still_blocks(tmp_path: Path) -> None:
    """The licence check is the one this most needs to keep.

    ``docs/anima_mvp.md`` promises "Preflight refuses to generate until this is
    set". Before this, any run without a ComfyUI client skipped that promise.
    """

    report = AnimaPreflight(None).run(
        _request(tmp_path, workflow=None, license_acknowledged=False)
    )

    assert "license.not_acknowledged" in report.codes()
    assert not report.ok
    with pytest.raises(PreflightError):
        report.raise_if_blocked()


def test_without_capabilities_a_remote_endpoint_without_auth_still_blocks(
    tmp_path: Path,
) -> None:
    report = AnimaPreflight(None).run(
        _request(
            tmp_path,
            workflow=None,
            comfy_base_url="http://10.0.0.5:8188",
            allow_remote_comfyui=True,
            comfy_auth_configured=False,
        )
    )

    assert "comfy.remote_without_auth" in report.codes()
    assert not report.ok


def test_a_workflow_without_capabilities_is_still_not_checked(tmp_path: Path) -> None:
    """Both halves are needed; a workflow alone cannot be verified."""

    report = AnimaPreflight(None).run(_request(tmp_path))

    assert CAPABILITIES_UNAVAILABLE in report.codes()


def test_capabilities_present_still_reports_no_unavailable_marker(tmp_path: Path) -> None:
    report = _preflight().run(_request(tmp_path))

    assert CAPABILITIES_UNAVAILABLE not in report.codes()
