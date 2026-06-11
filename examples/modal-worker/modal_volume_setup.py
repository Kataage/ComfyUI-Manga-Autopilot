"""Modal Volume setup helper — model manifest and volume layout tools.

Pure-Python helpers for validating and preparing Modal Volume storage
used by the Modal ComfyUI worker.  No Modal SDK required.

Usage as CLI::

    python examples/modal-worker/modal_volume_setup.py \\
      --manifest examples/modal-worker/model_manifest.example.json \\
      --local-root ./modal-volume-local \\
      --volume-name manga-autopilot-comfyui \\
      --print-commands
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------- manifest loading


def load_model_manifest(path: str | Path) -> dict[str, Any]:
    """Load a model manifest JSON file.

    Returns the parsed manifest dict.  Raises ``FileNotFoundError``
    if the file does not exist, or ``json.JSONDecodeError`` if invalid.
    """
    with open(path) as f:
        return json.load(f)


# --------------------------------------------------- manifest validation


def validate_model_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate a model manifest structure.

    Checks for required top-level keys and valid ``models`` list.

    Returns a preflight-style result dict.
    """
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    # version.
    version = manifest.get("version")
    if version is not None:
        checks.append({
            "name": "manifest_version",
            "ok": True,
            "message": f"manifest version: {version}",
            "details": {"version": version},
        })
    else:
        checks.append({
            "name": "manifest_version",
            "ok": False,
            "message": "missing manifest version",
        })
        errors.append("missing manifest version")

    # models list.
    models = manifest.get("models")
    if isinstance(models, list):
        checks.append({
            "name": "manifest_models",
            "ok": True,
            "message": f"models list present: {len(models)} entries",
            "details": {"count": len(models)},
        })

        # Validate each model entry.
        for i, model in enumerate(models):
            model_id = model.get("id", f"model_{i}")
            if not model.get("filename"):
                checks.append({
                    "name": f"manifest_model_{model_id}_filename",
                    "ok": False,
                    "message": f"model {model_id}: missing filename",
                })
                errors.append(f"model {model_id}: missing filename")
            if not model.get("relative_path"):
                checks.append({
                    "name": f"manifest_model_{model_id}_path",
                    "ok": False,
                    "message": f"model {model_id}: missing relative_path",
                })
                errors.append(f"model {model_id}: missing relative_path")
    else:
        checks.append({
            "name": "manifest_models",
            "ok": False,
            "message": "missing or invalid models list",
        })
        errors.append("missing or invalid models list")

    # workflows list.
    workflows = manifest.get("workflows")
    if isinstance(workflows, list):
        checks.append({
            "name": "manifest_workflows",
            "ok": True,
            "message": f"workflows list present: {len(workflows)} entries",
            "details": {"count": len(workflows)},
        })
    else:
        checks.append({
            "name": "manifest_workflows",
            "ok": False,
            "message": "missing or invalid workflows list",
        })
        errors.append("missing or invalid workflows list")

    return {
        "ok": len(errors) == 0,
        "checks": checks,
        "errors": errors,
    }


# --------------------------------------------------- volume layout validation


def validate_volume_layout(
    volume_root: str | Path,
    manifest: dict[str, Any],
    *,
    check_sha256: bool = True,
) -> dict[str, Any]:
    """Validate that a local directory matches the model manifest.

    Checks that required model files exist and optionally verifies
    SHA-256 checksums.

    Parameters
    ----------
    volume_root:
        Path to the local volume directory (e.g. ``./modal-volume-local``).
    manifest:
        Model manifest dict.
    check_sha256:
        If ``True``, verify SHA-256 when specified in manifest.

    Returns
    -------
    dict
        Preflight-style result with checks for each model.
    """
    root = Path(volume_root)
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    models = manifest.get("models", [])
    for model in models:
        model_id = model.get("id", "unknown")
        rel_path = model.get("relative_path", "")
        required = model.get("required", False)
        expected_sha256 = model.get("sha256")

        file_path = root / rel_path
        if file_path.is_file():
            details: dict[str, Any] = {
                "path": str(file_path),
                "size_bytes": file_path.stat().st_size,
            }

            # SHA-256 check.
            if check_sha256 and expected_sha256:
                actual_sha = calculate_sha256(file_path)
                if actual_sha == expected_sha256:
                    checks.append({
                        "name": f"volume_model_{model_id}",
                        "ok": True,
                        "message": f"model {model_id}: exists, sha256 matches",
                        "details": {**details, "sha256": actual_sha},
                    })
                else:
                    msg = (
                        f"model {model_id}: sha256 mismatch "
                        f"(expected {expected_sha256}, got {actual_sha})"
                    )
                    checks.append({
                        "name": f"volume_model_{model_id}",
                        "ok": False,
                        "message": msg,
                    })
                    errors.append(msg)
            else:
                checks.append({
                    "name": f"volume_model_{model_id}",
                    "ok": True,
                    "message": f"model {model_id}: exists",
                    "details": details,
                })
        elif required:
            msg = f"model {model_id}: required file not found: {rel_path}"
            checks.append({
                "name": f"volume_model_{model_id}",
                "ok": False,
                "message": msg,
            })
            errors.append(msg)
        else:
            checks.append({
                "name": f"volume_model_{model_id}",
                "ok": True,
                "message": f"model {model_id}: optional, not present",
                "details": {"path": str(file_path)},
            })

    # Validate workflows.
    workflows = manifest.get("workflows", [])
    for wf in workflows:
        wf_id = wf.get("workflow_id", "unknown")
        registry_path = wf.get("registry_path", "")
        if registry_path:
            file_path = root / registry_path
            if file_path.is_file():
                checks.append({
                    "name": f"volume_workflow_{wf_id}",
                    "ok": True,
                    "message": f"workflow {wf_id}: registry exists",
                    "details": {"path": str(file_path)},
                })
            else:
                msg = f"workflow {wf_id}: registry not found: {registry_path}"
                checks.append({
                    "name": f"volume_workflow_{wf_id}",
                    "ok": False,
                    "message": msg,
                })
                errors.append(msg)

    return {
        "ok": len(errors) == 0,
        "checks": checks,
        "errors": errors,
    }


# --------------------------------------------------- SHA-256


def calculate_sha256(path: str | Path) -> str:
    """Calculate the SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------- modal volume put commands


def build_modal_volume_put_commands(
    volume_name: str,
    manifest: dict[str, Any],
    local_root: str | Path,
) -> list[str]:
    """Generate ``modal volume put`` commands for each model/workflow.

    Returns a list of command strings.  Does **not** execute them.

    Parameters
    ----------
    volume_name:
        Modal Volume name (e.g. ``manga-autopilot-comfyui``).
    manifest:
        Model manifest dict.
    local_root:
        Path to the local directory containing model files.
    """
    root = Path(local_root)
    commands: list[str] = []

    models = manifest.get("models", [])
    for model in models:
        rel_path = model.get("relative_path", "")
        if not rel_path:
            continue
        local_path = root / rel_path
        remote_path = f"/{rel_path}"
        if local_path.is_file():
            commands.append(
                f"modal volume put {volume_name} {local_path} {remote_path}"
            )

    workflows = manifest.get("workflows", [])
    for wf in workflows:
        for key in ("registry_path", "workflow_path"):
            rel_path = wf.get(key, "")
            if not rel_path:
                continue
            local_path = root / rel_path
            remote_path = f"/{rel_path}"
            if local_path.is_file():
                commands.append(
                    f"modal volume put {volume_name} {local_path} {remote_path}"
                )

    return commands


# --------------------------------------------------- CLI


def main() -> None:
    """CLI entry point for volume setup helper."""
    parser = argparse.ArgumentParser(
        description="Modal Volume setup helper — validate manifest and generate upload commands."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to model_manifest JSON file.",
    )
    parser.add_argument(
        "--local-root",
        required=True,
        help="Path to local volume directory.",
    )
    parser.add_argument(
        "--volume-name",
        default="manga-autopilot-comfyui",
        help="Modal Volume name (default: manga-autopilot-comfyui).",
    )
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print modal volume put commands instead of executing.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate manifest and volume layout.",
    )
    args = parser.parse_args()

    # Load manifest.
    try:
        manifest = load_model_manifest(args.manifest)
    except Exception as exc:
        print(f"Error loading manifest: {exc}", file=sys.stderr)
        sys.exit(1)

    # Validate manifest.
    manifest_result = validate_model_manifest(manifest)
    if not manifest_result["ok"]:
        print("Manifest validation failed:", file=sys.stderr)
        for err in manifest_result["errors"]:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)
    print(f"Manifest OK: {len(manifest_result['checks'])} checks passed")

    # Validate volume layout.
    local_root = Path(args.local_root)
    if local_root.is_dir():
        layout_result = validate_volume_layout(local_root, manifest)
        if not layout_result["ok"]:
            print("Volume layout validation failed:", file=sys.stderr)
            for err in layout_result["errors"]:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)
        print(f"Volume layout OK: {len(layout_result['checks'])} checks passed")
    else:
        print(f"Warning: local root not found: {local_root}")

    if args.validate_only:
        return

    # Generate commands.
    commands = build_modal_volume_put_commands(
        args.volume_name, manifest, local_root
    )
    if commands:
        print(f"\nGenerated {len(commands)} upload commands:")
        for cmd in commands:
            print(f"  {cmd}")
    else:
        print("\nNo files to upload.")


if __name__ == "__main__":
    main()
