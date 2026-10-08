"""Export an authoritative v2 Work Page without client-supplied layout or image paths.

The persisted Page/LayoutSlot/Panel snapshot is the same Page application
projection read by Page Editor. All candidate images must be registered,
verified READY Artifacts owned by the Panel. Legacy page renderer is reused,
but rendered bytes are registered through the immutable Work ArtifactRepository.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
import threading
from pathlib import Path
from typing import Any

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.primitives import canonical_json, new_id
from manga_autopilot.repositories import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRepository,
    PageDomainNotFoundError,
    WorkLifecycleRepository,
)
from manga_autopilot.services.page_application import (
    PageApplicationService,
    read_page_state_in_transaction,
)
from manga_autopilot.services.page_png_budget import (
    DEFAULT_PROFILE,
    resolve_page_png_budget,
)
from manga_autopilot.services.page_renderer import render_page_to_png
from manga_autopilot.storage import assert_managed_path


class PageExportValidationError(ValueError):
    """A saved layout or selected image cannot be exported safely."""


class PageExportConflictError(PageExportValidationError):
    """Persisted state changed during rendering; reject the stale export."""


class PageExportBusyError(PageExportValidationError):
    """This worker is already rendering another large Work Page PNG."""


# Process-local backpressure: concurrent requests through multiple service
# instances in one worker must not allocate multiple giant Pillow canvases.
# Multi-worker deployments must provision/enforce a separate global capacity.
_RENDER_SLOT = threading.BoundedSemaphore(value=1)


def _number(data: dict[str, Any], field: str, label: str) -> float:
    value = data.get(field)
    if type(value) not in {int, float} or not math.isfinite(value):
        raise PageExportValidationError(
            f"{label}: persisted geometry.{field} must be a finite number"
        )
    return float(value)


def _size(data: dict[str, Any], field: str, label: str) -> int:
    value = _number(data, field, label)
    if value < 1 or value > 16384 or not value.is_integer():
        raise PageExportValidationError(
            f"{label}: persisted geometry.{field} must be an integer from 1 to 16384"
        )
    return int(value)


def _selected_artifact(
    repository: ArtifactRepository,
    panel: dict[str, Any],
) -> dict[str, Any]:
    panel_id = str(panel["id"])
    selected = panel["selected_candidate_id"]
    if selected:
        # W0005 has no panel_candidates join table. For this vertical slice,
        # a selected candidate must identify its immutable Artifact by ID.
        # It is *not* safe to guess a different candidate or a file on disk.
        try:
            artifact = repository.get(selected)
        except ArtifactNotFoundError as exc:
            raise PageExportValidationError(
                f"Panel {panel_id}: selected candidate {selected!r} has no registered "
                "Artifact. Register an Artifact with this ID for this Panel."
            ) from exc
    else:
        candidates = [
            record for record in repository.list_for_scope("panel", panel_id)
            if record["artifact_type"] == "panel_candidate"
            and record["status"] == "READY"
            and record["archived_at"] is None
        ]
        if len(candidates) != 1:
            raise PageExportValidationError(
                f"Panel {panel_id}: expected exactly one current READY panel_candidate "
                f"Artifact, found {len(candidates)}. Select a candidate explicitly "
                "or register the missing image."
            )
        artifact = candidates[0]

    if (artifact["artifact_type"] != "panel_candidate"
        or artifact["scope_type"] != "panel"
        or artifact["scope_id"] != panel_id
        or artifact["status"] != "READY"
        or artifact["archived_at"] is not None
        or artifact["mime_type"] not in {"image/png", "image/jpeg", "image/webp"}):
        raise PageExportValidationError(
            f"Panel {panel_id}: Artifact {artifact['id']} is not a current "
            "READY image owned by this Panel."
        )
    try:
        repository.verify_registered_file(str(artifact["id"]))
    except (ArtifactIntegrityError, ValueError, FileNotFoundError) as exc:
        raise PageExportValidationError(
            f"Panel {panel_id}: registered Artifact {artifact['id']} file is "
            f"missing, unsafe, or corrupt: {exc}"
        ) from exc
    return artifact


def _snapshot_fingerprint(state: dict[str, Any]) -> str:
    """Hash the persisted state that determines layout and image selection."""
    payload = {
        "page": state["page"],
        "layout": state["layout"],
        "slots": state["slots"],
        "panels": state["panels"],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class WorkPageExportService:
    """Render a persisted v2 Page and register its immutable PNG Artifact."""

    def __init__(self, storage_root: str | Path) -> None:
        self.storage_root = Path(storage_root)
        self.pages = PageApplicationService(storage_root)

    def export_png(
        self,
        work_id: str,
        page_id: str,
        *,
        background: str = "#ffffff",
        outer_border: bool = True,
        export_profile: str = DEFAULT_PROFILE,
    ) -> dict[str, Any]:
        if not _RENDER_SLOT.acquire(blocking=False):
            raise PageExportBusyError(
                f"Work {work_id}, Page {page_id}: another PNG render is in progress "
                "in this worker. Retry after it completes."
            )
        try:
            return self._export_png(
                work_id, page_id, background=background,
                outer_border=outer_border, export_profile=export_profile,
            )
        finally:
            _RENDER_SLOT.release()

    def _export_png(
        self,
        work_id: str,
        page_id: str,
        *,
        background: str,
        outer_border: bool,
        export_profile: str,
    ) -> dict[str, Any]:
        try:
            budget = resolve_page_png_budget(export_profile)
        except ValueError as exc:
            raise PageExportValidationError(str(exc)) from exc
        if (not isinstance(background, str) or len(background) != 7
            or background[0] != "#" or any(
                ch not in "0123456789abcdefABCDEF" for ch in background[1:]
            )):
            raise PageExportValidationError(
                "background must be a hexadecimal #RRGGBB color"
            )
        if type(outer_border) is not bool:
            raise PageExportValidationError("outer_border must be a boolean")

        state = self.pages.get_page(work_id, page_id)
        layout = state["layout"]
        if layout is None:
            raise PageExportValidationError(
                f"Page {page_id}: no persisted Layout. Save a Layout before export."
            )
        page_geometry = layout["geometry_json"]
        if not isinstance(page_geometry, dict):
            raise PageExportValidationError("persisted Layout geometry is not an object")
        width = _size(page_geometry, "width", f"Page {page_id}")
        height = _size(page_geometry, "height", f"Page {page_id}")
        area = width * height
        if area > budget.max_pixels:
            raise PageExportValidationError(
                f"Work {work_id}, Page {page_id}: {width} x {height} "
                f"({area:,} pixels) exceeds {budget.name!r} export_profile "
                f"limit of {budget.max_pixels:,} pixels. Reduce saved Page "
                "dimensions in Page Editor or explicitly select the 'print' "
                "profile for a larger permitted output."
            )

        slots = {slot["id"]: slot for slot in state["slots"]}
        if not state["panels"]:
            raise PageExportValidationError(
                f"Page {page_id}: no persisted Panels available to export"
            )
        artifact_repo = ArtifactRepository(self.storage_root, work_id)
        work = WorkLifecycleRepository(self.storage_root).open_work(work_id)
        layouts: list[PanelLayout] = []
        dependencies: list[dict[str, Any]] = []
        total_input_pixels = 0
        for panel in state["panels"]:
            panel_id = panel["id"]
            slot_id = panel["layout_slot_id"]
            if not slot_id or slot_id not in slots:
                raise PageExportValidationError(
                    f"Panel {panel_id}: no LayoutSlot binding in Page {page_id}. "
                    "Bind this Panel to a saved LayoutSlot in Page Editor."
                )
            slot = slots[slot_id]
            shape = slot["geometry_json"]
            if not isinstance(shape, dict):
                raise PageExportValidationError(
                    f"LayoutSlot {slot_id}: saved geometry is not an object"
                )
            x = _number(shape, "x", f"LayoutSlot {slot_id}")
            y = _number(shape, "y", f"LayoutSlot {slot_id}")
            sw = _number(shape, "width", f"LayoutSlot {slot_id}")
            sh = _number(shape, "height", f"LayoutSlot {slot_id}")
            if sw <= 0 or sh <= 0:
                raise PageExportValidationError(
                    f"LayoutSlot {slot_id}: width and height must be positive"
                )
            artifact = _selected_artifact(artifact_repo, panel)
            iw, ih = artifact["width"], artifact["height"]
            if type(iw) is not int or type(ih) is not int or iw < 1 or ih < 1:
                raise PageExportValidationError(
                    f"Panel {panel_id}: selected Artifact has no valid stored image dimensions"
                )
            input_area = iw * ih
            total_input_pixels += input_area
            if input_area > budget.max_input_pixels or (
                total_input_pixels > budget.max_total_input_pixels
            ):
                raise PageExportValidationError(
                    f"Work {work_id}, Page {page_id}, Panel {panel_id}: "
                    "registered candidate image input pixel budget exceeded "
                    f"for export_profile {budget.name!r}. Use smaller images "
                    "or the explicit 'print' profile."
                )
            image_path = assert_managed_path(
                work.root.joinpath(*artifact["relative_path"].split("/")),
                containment_root=work.root,
                field_name=f"Panel {panel_id} selected Artifact",
            )
            layouts.append(
                PanelLayout(
                    panel_id=panel_id, x=x, y=y, width=sw, height=sh,
                    z_index=panel["order_index"], image_path=str(image_path),
                )
            )
            dependencies.append({
                "panel_id": panel_id,
                "panel_revision": panel["revision"],
                "slot_id": slot_id,
                "slot_revision": slot["revision"],
                "artifact_id": artifact["id"],
                "sha256": artifact["sha256"],
            })

        dependencies.sort(key=lambda row: row["panel_id"])
        fingerprint_payload = {
            "page_id": page_id,
            "page_state_sha256": _snapshot_fingerprint(state),
            "panel_artifacts": dependencies,
            "background": background,
            "outer_border": outer_border,
            "export_profile": budget.name,
            "renderer": "legacy_page_renderer_v1",
        }
        fingerprint = hashlib.sha256(
            canonical_json(fingerprint_payload).encode("utf-8")
        ).hexdigest()

        # Preserve Panel order for the commit guard. The fingerprint list is
        # sorted by Panel ID for stable provenance independently of page order.
        dependencies_by_order = [
            next(item for item in dependencies if item["panel_id"] == panel["id"])
            for panel in state["panels"]
        ]

        # Renderer writes only into a disposable private directory; it never
        # replaces an existing Work export. ArtifactRepository publishes the
        # validated output to an immutable Work path and commits provenance.
        temp_root = assert_managed_path(
            work.root / "assets" / "temp",
            containment_root=work.root,
            field_name="Work render temp directory",
        )
        temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="page-export-", dir=temp_root) as render_dir:
            result = render_page_to_png(
                f"page_{state['page']['page_number']}",
                layouts,
                output_dir=render_dir,
                page_width=width,
                page_height=height,
                background=background,
                outer_border=outer_border,
            )
            # Fail before final Artifact publication, including pathological
            # incompressible PNGs that exceed the profile's disk/network budget.
            output_bytes = result.output_path.stat().st_size
            if output_bytes > budget.max_png_bytes:
                raise PageExportValidationError(
                    f"Work {work_id}, Page {page_id}: rendered PNG is "
                    f"{output_bytes:,} bytes, exceeding export_profile "
                    f"{budget.name!r} limit of {budget.max_png_bytes:,} bytes."
                )
            if result.images_composited != len(layouts):
                raise PageExportValidationError(
                    f"Page {page_id}: only {result.images_composited}/"
                    f"{len(layouts)} selected images could be composited."
                )
            # This compares the same persisted Page snapshot consumed by
            # Page Editor; reject renders overtaken by a concurrent edit.
            if _snapshot_fingerprint(self.pages.get_page(work_id, page_id)) != (
                fingerprint_payload["page_state_sha256"]
            ):
                raise PageExportConflictError(
                    f"Page {page_id} changed while rendering; reload and export again."
                )
            for dependency in dependencies:
                artifact_repo.verify_registered_file(dependency["artifact_id"])
            # Recheck the EXACT persisted input projection inside the final
            # Artifact registration's BEGIN IMMEDIATE transaction. The earlier
            # post-render read is only a fast-fail optimization: it cannot
            # protect the window while output bytes are copied and published.
            def guard_source_revision(conn):
                try:
                    current = read_page_state_in_transaction(
                        conn, work.work_id, page_id
                    )
                except PageDomainNotFoundError as exc:
                    raise PageExportConflictError(
                        f"Page {page_id} disappeared before PNG commit; retry export."
                    ) from exc
                if _snapshot_fingerprint(current) != (
                    fingerprint_payload["page_state_sha256"]
                ):
                    raise PageExportConflictError(
                        f"Page {page_id} changed before PNG commit; "
                        "reload and export again."
                    )
                # Artifacts are immutable, but candidate publication itself
                # changes implicit selection when no ID was pinned to a Panel.
                # Verify the selected inputs and unique fallback under the
                # same lock, without a nested repository/file-system read.
                for panel, dependency in zip(current["panels"], dependencies_by_order):
                    row = conn.execute(
                        """SELECT artifact_type, scope_type, scope_id,
                                  status, archived_at, mime_type, sha256
                           FROM artifacts WHERE id = ?""",
                        (dependency["artifact_id"],),
                    ).fetchone()
                    if row is None or (
                        row["artifact_type"] != "panel_candidate"
                        or row["scope_type"] != "panel"
                        or row["scope_id"] != panel["id"]
                        or row["status"] != "READY"
                        or row["archived_at"] is not None
                        or row["mime_type"] not in {
                            "image/png", "image/jpeg", "image/webp"
                        }
                        or row["sha256"] != dependency["sha256"]
                    ):
                        raise PageExportConflictError(
                            f"Page {page_id} candidate inputs changed before PNG commit."
                        )
                    if panel["selected_candidate_id"] is None:
                        available = conn.execute(
                            """SELECT id FROM artifacts
                               WHERE artifact_type = 'panel_candidate'
                                 AND scope_type = 'panel' AND scope_id = ?
                                 AND status = 'READY' AND archived_at IS NULL""",
                            (panel["id"],),
                        ).fetchall()
                        if len(available) != 1 or available[0]["id"] != (
                            dependency["artifact_id"]
                        ):
                            raise PageExportConflictError(
                                f"Page {page_id} candidate choice changed before "
                                "PNG commit; select a candidate and retry."
                            )

            artifact_id = new_id("artifact")
            relative_path = f"exports/pages/{page_id}_{artifact_id}.png"
            artifact = artifact_repo.register_local_file(
                source_path=result.output_path,
                relative_path=relative_path,
                artifact_type="page_render",
                scope_type="page",
                scope_id=page_id,
                mime_type="image/png",
                dependency_fingerprint=fingerprint,
                artifact_id=artifact_id,
                commit_guard=guard_source_revision,
            )
        return {
            "work_id": work_id,
            "page_id": page_id,
            "artifact_id": artifact["id"],
            "relative_path": artifact["relative_path"],
            "sha256": artifact["sha256"],
            "file_size": artifact["file_size"],
            "width": artifact["width"],
            "height": artifact["height"],
            "panels_drawn": result.panels_drawn,
            "images_composited": result.images_composited,
            "dependency_fingerprint": fingerprint,
        }


__all__ = [
    "PageExportBusyError",
    "PageExportConflictError",
    "PageExportValidationError",
    "WorkPageExportService",
]
