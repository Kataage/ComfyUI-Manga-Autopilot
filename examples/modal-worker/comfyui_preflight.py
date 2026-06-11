"""Modal ComfyUI preflight validation helpers.

Pure-Python helpers for validating the Modal ComfyUI execution
environment before running a workflow.  No Modal SDK or GPU required.

These functions return structured check results that can be returned
directly from the ``/v1/preflight`` endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

# --------------------------------------------------------- response builders


def _check(
    name: str,
    ok: bool,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a single check result dict."""
    result: dict[str, Any] = {"name": name, "ok": ok, "message": message}
    if details:
        result["details"] = details
    return result


def _preflight_result(
    checks: list[dict[str, Any]],
    *,
    executor: str = "modal-comfyui",
) -> dict[str, Any]:
    """Build a preflight result from a list of checks."""
    errors = [c["message"] for c in checks if not c["ok"]]
    return {
        "ok": len(errors) == 0,
        "executor": executor,
        "checks": checks,
        "errors": errors,
    }


# --------------------------------------------------- env validation


REQUIRED_ENV_VARS = (
    "MANGA_AUTOPILOT_MODAL_VOLUME_NAME",
    "MANGA_MODAL_COMFYUI_ROOT",
)


def validate_modal_comfyui_env(
    env: Mapping[str, str],
    *,
    required: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Validate Modal ComfyUI environment variables.

    Parameters
    ----------
    env:
        Mapping of environment variable names to values
        (e.g. ``os.environ``).
    required:
        Override the list of required env vars.  Defaults to
        ``REQUIRED_ENV_VARS``.

    Returns
    -------
    dict
        Preflight result with checks for each required variable.
    """
    required = required or REQUIRED_ENV_VARS
    checks: list[dict[str, Any]] = []
    for var in required:
        val = env.get(var, "").strip()
        if val:
            checks.append(_check(
                f"env_{var}",
                True,
                f"{var} is set",
                details={"value": val},
            ))
        else:
            checks.append(_check(
                f"env_{var}",
                False,
                f"{var} is not set",
            ))
    return _preflight_result(checks)


# --------------------------------------------------- path validation


def validate_comfyui_paths(
    *,
    comfyui_root: str | Path,
    checkpoints_dir: str | Path | None = None,
    workflows_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Validate ComfyUI directory structure.

    Checks that ``comfyui_root`` exists and is a directory, and
    optionally validates ``checkpoints_dir`` and ``workflows_dir``.
    """
    checks: list[dict[str, Any]] = []

    # ComfyUI root.
    root = Path(comfyui_root)
    if root.is_dir():
        checks.append(_check(
            "comfyui_root",
            True,
            f"ComfyUI root exists: {root}",
            details={"path": str(root)},
        ))
    else:
        checks.append(_check(
            "comfyui_root",
            False,
            f"ComfyUI root not found: {root}",
        ))

    # Checkpoints directory.
    if checkpoints_dir is not None:
        ckpt_dir = Path(checkpoints_dir)
        if ckpt_dir.is_dir():
            checks.append(_check(
                "checkpoints_dir",
                True,
                f"Checkpoints directory exists: {ckpt_dir}",
                details={"path": str(ckpt_dir)},
            ))
        else:
            # Also check under comfyui_root/models/checkpoints.
            default_ckpt_dir = root / "models" / "checkpoints"
            if default_ckpt_dir.is_dir():
                checks.append(_check(
                    "checkpoints_dir",
                    True,
                    f"Checkpoints directory exists: {default_ckpt_dir}",
                    details={"path": str(default_ckpt_dir)},
                ))
            else:
                checks.append(_check(
                    "checkpoints_dir",
                    False,
                    f"Checkpoints directory not found: {ckpt_dir}",
                ))

    # Workflows directory.
    if workflows_dir is not None:
        wf_dir = Path(workflows_dir)
        if wf_dir.is_dir():
            checks.append(_check(
                "workflows_dir",
                True,
                f"Workflows directory exists: {wf_dir}",
                details={"path": str(wf_dir)},
            ))
        else:
            checks.append(_check(
                "workflows_dir",
                False,
                f"Workflows directory not found: {wf_dir}",
            ))

    return _preflight_result(checks)


# ----------------------------------------------- checkpoint validation


def validate_checkpoint_exists(
    *,
    checkpoints_dir: str | Path,
    checkpoint_name: str,
) -> dict[str, Any]:
    """Validate that a checkpoint file exists.

    Parameters
    ----------
    checkpoints_dir:
        Directory containing checkpoint files.
    checkpoint_name:
        Name of the checkpoint file (e.g. ``example.safetensors``).
    """
    ckpt_dir = Path(checkpoints_dir)
    ckpt_path = ckpt_dir / checkpoint_name

    if ckpt_path.is_file():
        size_mb = ckpt_path.stat().st_size / (1024 * 1024)
        return _preflight_result([
            _check(
                "checkpoint_exists",
                True,
                f"checkpoint found: {checkpoint_name}",
                details={
                    "path": str(ckpt_path),
                    "size_mb": round(size_mb, 1),
                },
            )
        ])
    else:
        # List available checkpoints for helpful error message.
        available: list[str] = []
        if ckpt_dir.is_dir():
            available = [
                f.name for f in ckpt_dir.iterdir()
                if f.is_file() and f.suffix in (".safetensors", ".ckpt", ".pt")
            ]
        msg = f"checkpoint not found: {checkpoint_name}"
        if available:
            msg += f" (available: {', '.join(available[:5])})"
        return _preflight_result([
            _check("checkpoint_exists", False, msg)
        ])


# ----------------------------------------------- workflow validation


def validate_workflow_registry(
    registry: dict[str, Any],
) -> dict[str, Any]:
    """Validate a workflow registry JSON structure.

    Checks for required top-level keys: ``workflow_id``, ``bindings``,
    ``api_graph``.
    """
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    # workflow_id.
    wf_id = registry.get("workflow_id")
    if wf_id:
        checks.append(_check(
            "workflow_id",
            True,
            f"workflow_id: {wf_id}",
            details={"workflow_id": wf_id},
        ))
    else:
        checks.append(_check("workflow_id", False, "missing workflow_id"))
        errors.append("missing workflow_id")

    # bindings.
    bindings = registry.get("bindings")
    if isinstance(bindings, dict) and bindings:
        checks.append(_check(
            "workflow_bindings",
            True,
            f"bindings present: {len(bindings)} keys",
            details={"keys": list(bindings.keys())},
        ))
    else:
        checks.append(_check(
            "workflow_bindings",
            False,
            "missing or empty bindings",
        ))
        errors.append("missing or empty bindings")

    # api_graph.
    api_graph = registry.get("api_graph")
    if isinstance(api_graph, dict) and api_graph:
        checks.append(_check(
            "workflow_api_graph",
            True,
            f"api_graph present: {len(api_graph)} nodes",
            details={"node_ids": list(api_graph.keys())},
        ))
    else:
        checks.append(_check(
            "workflow_api_graph",
            False,
            "missing or empty api_graph",
        ))
        errors.append("missing or empty api_graph")

    return _preflight_result(checks)


def validate_workflow_bindings(
    registry: dict[str, Any],
    required_bindings: list[str] | None = None,
) -> dict[str, Any]:
    """Validate that required workflow bindings are present.

    Parameters
    ----------
    registry:
        Workflow registry dict (must have ``bindings`` key).
    required_bindings:
        List of binding keys that must be present.  Defaults to the
        standard Manga Autopilot bindings.
    """
    if required_bindings is None:
        required_bindings = [
            "positive_prompt",
            "negative_prompt",
            "seed",
            "width",
            "height",
        ]

    bindings = registry.get("bindings", {})
    checks: list[dict[str, Any]] = []

    for binding_key in required_bindings:
        if binding_key in bindings:
            binding = bindings[binding_key]
            node_id = binding.get("node_id", "?")
            input_name = binding.get("input", "?")
            checks.append(_check(
                f"binding_{binding_key}",
                True,
                f"{binding_key} -> node {node_id}.{input_name}",
                details={"node_id": str(node_id), "input": input_name},
            ))
        else:
            checks.append(_check(
                f"binding_{binding_key}",
                False,
                f"missing required binding: {binding_key}",
            ))

    return _preflight_result(checks)


def detect_checkpoint_from_registry(
    registry: dict[str, Any],
) -> str | None:
    """Best-effort detection of checkpoint name from workflow registry.

    Looks for a ``CheckpointLoaderSimple`` node and reads its
    ``ckpt_name`` input.  Returns ``None`` if not found.
    """
    bindings = registry.get("bindings", {})
    api_graph = registry.get("api_graph", {})

    # Method 1: checkpoint binding.
    if "checkpoint" in bindings:
        ckpt_binding = bindings["checkpoint"]
        node_id = str(ckpt_binding["node_id"])
        input_name = ckpt_binding.get("input", "ckpt_name")
        node = api_graph.get(node_id, {})
        inputs = node.get("inputs", {})
        val = inputs.get(input_name)
        if isinstance(val, str) and val:
            return val

    # Method 2: scan for CheckpointLoaderSimple nodes.
    for _node_id, node in api_graph.items():
        if node.get("class_type") == "CheckpointLoaderSimple":
            ckpt_name = node.get("inputs", {}).get("ckpt_name")
            if isinstance(ckpt_name, str) and ckpt_name:
                return ckpt_name

    return None


# ----------------------------------------------- combined preflight


def run_preflight(
    *,
    env: Mapping[str, str] | None = None,
    comfyui_root: str | Path | None = None,
    checkpoints_dir: str | Path | None = None,
    checkpoint_name: str | None = None,
    workflow_registry: dict[str, Any] | None = None,
    required_bindings: list[str] | None = None,
) -> dict[str, Any]:
    """Run all preflight checks and return a combined result.

    This is a convenience function that runs all individual validations
    and merges the results.
    """
    import os

    if env is None:
        env = os.environ

    all_checks: list[dict[str, Any]] = []
    all_errors: list[str] = []

    # 1. Environment variables.
    env_result = validate_modal_comfyui_env(env)
    all_checks.extend(env_result["checks"])
    all_errors.extend(env_result["errors"])

    # 2. Paths.
    root = comfyui_root or env.get("MANGA_MODAL_COMFYUI_ROOT", "/root/ComfyUI")
    ckpt_dir = checkpoints_dir or str(Path(root) / "models" / "checkpoints")
    path_result = validate_comfyui_paths(
        comfyui_root=root,
        checkpoints_dir=ckpt_dir,
    )
    all_checks.extend(path_result["checks"])
    all_errors.extend(path_result["errors"])

    # 3. Checkpoint.
    if checkpoint_name:
        ckpt_result = validate_checkpoint_exists(
            checkpoints_dir=ckpt_dir,
            checkpoint_name=checkpoint_name,
        )
        all_checks.extend(ckpt_result["checks"])
        all_errors.extend(ckpt_result["errors"])

    # 4. Workflow registry.
    if workflow_registry:
        wf_result = validate_workflow_registry(workflow_registry)
        all_checks.extend(wf_result["checks"])
        all_errors.extend(wf_result["errors"])

        # 5. Workflow bindings.
        bind_result = validate_workflow_bindings(
            workflow_registry,
            required_bindings,
        )
        all_checks.extend(bind_result["checks"])
        all_errors.extend(bind_result["errors"])

        # 6. Auto-detect checkpoint if not provided.
        if not checkpoint_name:
            detected = detect_checkpoint_from_registry(workflow_registry)
            if detected:
                ckpt_result = validate_checkpoint_exists(
                    checkpoints_dir=ckpt_dir,
                    checkpoint_name=detected,
                )
                all_checks.extend(ckpt_result["checks"])
                all_errors.extend(ckpt_result["errors"])

    return {
        "ok": len(all_errors) == 0,
        "executor": "modal-comfyui",
        "checks": all_checks,
        "errors": all_errors,
    }
