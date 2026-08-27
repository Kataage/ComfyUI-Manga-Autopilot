"""Read-only preflight for strict Anima runs.

Strict Anima generation is expensive and sequential, so every condition that
would fail the run is checked before the first panel is queued: the endpoint and
its authentication, the workflow's bindings against its own graph and against
the node classes ComfyUI actually registers, the model and LoRA files the profile
names, the character references, the resolution policy, the output directory, and
the licence acknowledgement.

Nothing here writes, downloads, loads a model, or queues work. Capabilities come
from a `/object_info` snapshot the caller has already fetched, which keeps the
whole check testable without a live server.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from manga_autopilot.models.generation_profile import GenerationProfile
from manga_autopilot.models.workflow import WorkflowDefinition
from manga_autopilot.services.generation_profiles import resolve_panel_resolution

log = logging.getLogger(__name__)

ERROR = "error"
WARNING = "warning"

LOOPBACK_HOSTS = frozenset({"localhost", "::1", "0.0.0.0"})

#: ComfyUI skips the negative branch when cond_scale is close to 1.0.
CFG1_TOLERANCE = 1e-6

#: Binding keys whose absence means the profile cannot enforce its own settings.
TECHNICAL_BINDING_KEYS: tuple[str, ...] = ("steps", "cfg", "seed", "width", "height")

#: Which `/object_info` combo input lists a given profile asset is drawn from.
UNET_FIELDS: tuple[str, ...] = ("unet_name", "ckpt_name")
TEXT_ENCODER_FIELDS: tuple[str, ...] = ("clip_name",)
VAE_FIELDS: tuple[str, ...] = ("vae_name",)
LORA_FIELDS: tuple[str, ...] = ("lora_name",)


@dataclass(frozen=True)
class PreflightIssue:
    """One thing preflight found. `code` is stable; `message` is for humans."""

    code: str
    message: str
    severity: str = ERROR


class PreflightError(RuntimeError):
    """Raised when a report carries at least one blocking error."""

    def __init__(self, report: PreflightReport) -> None:
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in report.errors)
        super().__init__(f"Anima preflight failed: {details}")
        self.report = report


@dataclass(frozen=True)
class PreflightReport:
    """The full result of one preflight run."""

    issues: tuple[PreflightIssue, ...] = ()

    @property
    def errors(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == ERROR)

    @property
    def warnings(self) -> tuple[PreflightIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == WARNING)

    @property
    def ok(self) -> bool:
        """Whether generation may start. Warnings do not block."""
        return not self.errors

    def codes(self) -> tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def raise_if_blocked(self) -> None:
        """Raise :class:`PreflightError` if anything blocking was found."""
        if not self.ok:
            raise PreflightError(self)


@dataclass(frozen=True)
class ComfyCapabilities:
    """What a ComfyUI install currently offers, distilled from `/object_info`."""

    node_classes: frozenset[str] = frozenset()
    files: Mapping[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_object_info(cls, info: Mapping[str, Any]) -> ComfyCapabilities:
        """Read node classes and combo option lists out of an `/object_info` body.

        Combo inputs arrive as ``[[option, ...], {config}]``; typed inputs arrive
        as ``["MODEL"]`` and carry no file list, so only the former contribute.
        """
        node_classes: set[str] = set()
        files: dict[str, set[str]] = {}
        for class_name, entry in info.items():
            if not isinstance(entry, dict):
                continue
            node_classes.add(class_name)
            required = entry.get("input", {}).get("required", {})
            if not isinstance(required, dict):
                continue
            for input_name, spec in required.items():
                if not isinstance(spec, (list, tuple)) or not spec:
                    continue
                options = spec[0]
                if not isinstance(options, list):
                    continue
                files.setdefault(input_name, set()).update(str(o) for o in options)
        return cls(
            node_classes=frozenset(node_classes),
            files={name: frozenset(values) for name, values in files.items()},
        )

    def has_file(self, fields: Sequence[str], name: str) -> bool:
        """Return whether `name` appears in any of the given combo input lists."""
        return any(name in self.files.get(field_name, frozenset()) for field_name in fields)

    def node_inputs(self, class_name: str) -> frozenset[str]:
        return frozenset(self.files.get(class_name, frozenset()))


@dataclass
class PreflightRequest:
    """Everything preflight needs. No credential value is ever passed in."""

    profile: GenerationProfile
    workflow: WorkflowDefinition
    output_dir: Path
    comfy_base_url: str = "http://127.0.0.1:8188"
    allow_remote_comfyui: bool = False
    comfy_auth_configured: bool = False
    license_acknowledged: bool = False
    panel_sizes: Sequence[tuple[float, float]] = ()
    required_reference_character_ids: Sequence[str] = ()
    available_reference_images: Mapping[str, Path] = field(default_factory=dict)


def is_loopback(base_url: str) -> bool:
    """Return whether `base_url` points at this machine."""
    host = (urlparse(base_url).hostname or "").lower()
    if host in LOOPBACK_HOSTS:
        return True
    return host.startswith("127.")


@dataclass
class AnimaPreflight:
    """Run every strict-Anima readiness check against a capability snapshot."""

    capabilities: ComfyCapabilities

    def run(self, request: PreflightRequest) -> PreflightReport:
        """Return a report. This never raises for a failed check; call `raise_if_blocked`."""
        issues: list[PreflightIssue] = []
        issues.extend(self._check_endpoint(request))
        issues.extend(self._check_license(request))
        issues.extend(self._check_negative_prompt_reachability(request))
        issues.extend(self._check_models(request))
        issues.extend(self._check_workflow(request))
        issues.extend(self._check_resolution(request))
        issues.extend(self._check_references(request))
        issues.extend(self._check_output_dir(request))

        report = PreflightReport(tuple(issues))
        log.info(
            "anima preflight for profile %s: %d error(s), %d warning(s)",
            request.profile.id,
            len(report.errors),
            len(report.warnings),
        )
        return report

    # ------------------------------------------------------------- endpoint
    def _check_endpoint(self, request: PreflightRequest) -> list[PreflightIssue]:
        if is_loopback(request.comfy_base_url):
            return []
        if not request.allow_remote_comfyui:
            return [
                PreflightIssue(
                    "comfy.remote_not_allowed",
                    f"{request.comfy_base_url} is not a loopback address and "
                    "security.allow_remote_comfyui is disabled",
                )
            ]
        if not request.comfy_auth_configured:
            return [
                PreflightIssue(
                    "comfy.remote_without_auth",
                    f"{request.comfy_base_url} is remote but no ComfyUI auth token is "
                    "configured; set comfyui.auth_token_env and populate that variable",
                )
            ]
        return []

    # -------------------------------------------------------------- licence
    def _check_license(self, request: PreflightRequest) -> list[PreflightIssue]:
        license_ = request.profile.license
        if license_.requires_acknowledgement and not request.license_acknowledged:
            return [
                PreflightIssue(
                    "license.not_acknowledged",
                    f"{license_.name} must be acknowledged before generating with "
                    f"{request.profile.id} (see {license_.url})",
                )
            ]
        return []

    # ------------------------------------------------------ negative prompt
    def _check_negative_prompt_reachability(
        self, request: PreflightRequest
    ) -> list[PreflightIssue]:
        """Warn when the profile's CFG makes the negative prompt do nothing.

        ComfyUI's ``sampling_function`` sets ``uncond_ = None`` when
        ``cond_scale`` is close to 1.0, so at CFG 1 the negative branch is not
        evaluated at all. The wiring still looks correct in the graph, which is
        what makes this worth saying out loud: the effect is zero, not small.
        Verified against comfy/samplers.py in ComfyUI 0.30.0 on 2026-08-27.
        """
        if abs(request.profile.generation.cfg - 1.0) > CFG1_TOLERANCE:
            return []
        return [
            PreflightIssue(
                "prompt.negative_inert_at_cfg1",
                f"profile {request.profile.id} renders at CFG "
                f"{request.profile.generation.cfg}, so ComfyUI skips the negative "
                "branch entirely and the negative prompt has no effect; express "
                "anything you need to suppress positively instead",
                WARNING,
            )
        ]

    # --------------------------------------------------------------- models
    def _check_models(self, request: PreflightRequest) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        assets = request.profile.assets
        for name, fields, label in (
            (assets.unet, UNET_FIELDS, "diffusion model"),
            (assets.text_encoder, TEXT_ENCODER_FIELDS, "text encoder"),
            (assets.vae, VAE_FIELDS, "VAE"),
        ):
            if name and not self.capabilities.has_file(fields, name):
                issues.append(
                    PreflightIssue(
                        "model.missing",
                        f"{label} {name!r} is not installed in ComfyUI; "
                        "install it manually (this project never downloads weights)",
                    )
                )
        for lora in assets.loras:
            if not self.capabilities.has_file(LORA_FIELDS, lora.name):
                issues.append(
                    PreflightIssue(
                        "lora.missing",
                        f"LoRA {lora.name!r} required by {request.profile.id} is not "
                        "installed in ComfyUI",
                    )
                )
        return issues

    # ------------------------------------------------------------- workflow
    def _check_workflow(self, request: PreflightRequest) -> list[PreflightIssue]:
        workflow = request.workflow
        graph = workflow.api_graph
        if not graph:
            return [
                PreflightIssue(
                    "workflow.api_graph_absent",
                    f"workflow {workflow.workflow_id!r} carries no api_graph, so its "
                    "bindings cannot be verified before queueing",
                )
            ]

        issues: list[PreflightIssue] = []
        for class_name in sorted({str(node.get("class_type", "")) for node in graph.values()}):
            if class_name and class_name not in self.capabilities.node_classes:
                issues.append(
                    PreflightIssue(
                        "workflow.node_class_unavailable",
                        f"node class {class_name!r} used by {workflow.workflow_id!r} is not "
                        "registered with this ComfyUI install",
                    )
                )

        for key, binding in workflow.bindings.items():
            node = graph.get(binding.node_id)
            if node is None:
                issues.append(
                    PreflightIssue(
                        "workflow.node_missing",
                        f"binding {key!r} points at node {binding.node_id!r}, which is not "
                        "in the workflow graph",
                    )
                )
                continue
            inputs = node.get("inputs", {})
            if isinstance(inputs, dict) and binding.input not in inputs:
                issues.append(
                    PreflightIssue(
                        "workflow.input_missing",
                        f"binding {key!r} points at input {binding.input!r} of node "
                        f"{binding.node_id!r}, which does not accept it",
                    )
                )

        unbound = [key for key in TECHNICAL_BINDING_KEYS if key not in workflow.bindings]
        if unbound:
            issues.append(
                PreflightIssue(
                    "workflow.technical_field_unbound",
                    f"workflow {workflow.workflow_id!r} does not bind {', '.join(unbound)}; "
                    f"profile {request.profile.id} cannot enforce those values",
                    WARNING,
                )
            )
        return issues

    # ----------------------------------------------------------- resolution
    def _check_resolution(self, request: PreflightRequest) -> list[PreflightIssue]:
        policy = request.profile.resolution
        issues: list[PreflightIssue] = []

        if policy.min_side > policy.max_side:
            issues.append(
                PreflightIssue(
                    "resolution.policy_invalid",
                    f"resolution policy of {request.profile.id} has min_side "
                    f"{policy.min_side} above max_side {policy.max_side}",
                )
            )
        for label, value in (("min_side", policy.min_side), ("max_side", policy.max_side)):
            if value % policy.multiple_of:
                issues.append(
                    PreflightIssue(
                        "resolution.policy_invalid",
                        f"resolution policy of {request.profile.id} has {label} {value}, "
                        f"which is not a multiple of {policy.multiple_of}",
                    )
                )
        if issues:
            return issues

        for width, height in request.panel_sizes:
            if width <= 0 or height <= 0:
                issues.append(
                    PreflightIssue(
                        "resolution.policy_invalid",
                        f"panel size {width}x{height} is not positive",
                    )
                )
                continue
            resolved = resolve_panel_resolution(width, height, policy)
            requested = width / height
            effective = resolved.width / resolved.height
            if abs(effective - requested) / requested > 0.02:
                issues.append(
                    PreflightIssue(
                        "resolution.aspect_clamped",
                        f"panel aspect {width}:{height} cannot be rendered inside "
                        f"{policy.min_side}-{policy.max_side}; it will render as "
                        f"{resolved.width}x{resolved.height}",
                        WARNING,
                    )
                )
        return issues

    # ----------------------------------------------------------- references
    def _check_references(self, request: PreflightRequest) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        for character_id in request.required_reference_character_ids:
            path = request.available_reference_images.get(character_id)
            if path is None:
                issues.append(
                    PreflightIssue(
                        "reference.missing",
                        f"character {character_id!r} requires a reference image but none "
                        "is registered",
                    )
                )
            elif not Path(path).exists():
                issues.append(
                    PreflightIssue(
                        "reference.missing",
                        f"reference image for character {character_id!r} is registered but "
                        f"the file is gone: {Path(path).name}",
                    )
                )
        return issues

    # --------------------------------------------------------------- output
    def _check_output_dir(self, request: PreflightRequest) -> list[PreflightIssue]:
        target = Path(request.output_dir)
        probe = target / ".preflight-write-test"
        try:
            target.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"")
        except OSError as exc:
            return [
                PreflightIssue(
                    "output.unwritable",
                    f"output directory {target} is not writable: {exc.strerror or exc}",
                )
            ]
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        return []


__all__ = [
    "CFG1_TOLERANCE",
    "ERROR",
    "LORA_FIELDS",
    "TECHNICAL_BINDING_KEYS",
    "TEXT_ENCODER_FIELDS",
    "UNET_FIELDS",
    "VAE_FIELDS",
    "WARNING",
    "AnimaPreflight",
    "ComfyCapabilities",
    "PreflightError",
    "PreflightIssue",
    "PreflightReport",
    "PreflightRequest",
    "is_loopback",
]
