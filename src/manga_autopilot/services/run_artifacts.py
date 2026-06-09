"""Mirror project-root outputs into per-run directories (issue #194).

On Autopilot finalize (completed / failed / cancelled) the latest
project-root outputs are copied into ``runs/{run_id}/`` so each run
has its own snapshot of the generation artefacts.  The project-root
layout is preserved as the latest-compatible view.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# Files to mirror when they exist on project root.
_JSON_FILES = (
    "generation_log.json",
    "manifest.json",
    "panels.json",
    "bubbles.json",
    "pages.json",
)

# Directories to mirror when they exist on project root.
_DIR_NAMES = ("jobs", "assets", "exports")


class MirrorError(Exception):
    """Raised when mirroring artefacts to the run directory fails."""


def mirror_latest_artifacts_to_run(project_root: Path, run_id: str) -> dict[str, str]:
    """Copy project-root outputs into ``runs/{run_id}/``.

    Returns a mapping of logical name → relative path for every item
    that was actually mirrored (missing sources are silently skipped).

    Raises :class:`MirrorError` on I/O failure.
    """
    project_root = Path(project_root)
    run_dir = project_root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    mirrored: dict[str, str] = {}

    # ---- JSON files ----
    for name in _JSON_FILES:
        src = project_root / name
        if not src.exists():
            continue
        dst = run_dir / name
        # Never overwrite run.json which may already contain richer metadata.
        if dst.exists() and name == "run.json":
            continue
        try:
            shutil.copy2(src, dst)
            mirrored[name] = f"runs/{run_id}/{name}"
        except OSError as exc:
            raise MirrorError(f"failed to copy {name}: {exc}") from exc

    # ---- Directories ----
    for dirname in _DIR_NAMES:
        src_dir = project_root / dirname
        if not src_dir.exists() or not src_dir.is_dir():
            continue
        dst_dir = run_dir / dirname
        try:
            if dst_dir.exists():
                shutil.rmtree(dst_dir)
            shutil.copytree(src_dir, dst_dir)
            mirrored[dirname] = f"runs/{run_id}/{dirname}"
        except OSError as exc:
            raise MirrorError(f"failed to copy directory {dirname}: {exc}") from exc

    log.info("mirrored %d artefacts for run %s", len(mirrored), run_id)
    return mirrored


def read_run_artifacts_summary(project_root: Path, run_id: str) -> dict[str, str | None]:
    """Build an artefact summary dict for inclusion in run.json.

    Returns a mapping of logical name → relative path (or ``None`` if
    the artefact does not exist in the run directory).
    """
    project_root = Path(project_root)
    run_dir = project_root / "runs" / run_id
    summary: dict[str, str | None] = {}

    for name in _JSON_FILES:
        p = run_dir / name
        summary[name] = f"runs/{run_id}/{name}" if p.exists() else None

    for dirname in _DIR_NAMES:
        p = run_dir / dirname
        summary[dirname] = f"runs/{run_id}/{dirname}" if p.exists() else None

    return summary


def inject_artifacts_root_to_manifest(project_root: Path, run_id: str) -> bool:
    """Add ``artifacts_root`` to the manifest.json at project root.

    Returns ``True`` if the manifest was updated, ``False`` if the
    manifest does not exist.
    """
    manifest_path = project_root / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    data["artifacts_root"] = f"runs/{run_id}"
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


__all__ = [
    "MirrorError",
    "inject_artifacts_root_to_manifest",
    "mirror_latest_artifacts_to_run",
    "read_run_artifacts_summary",
]
