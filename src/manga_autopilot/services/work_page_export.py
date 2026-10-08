"""Export an authoritative v2 Work Page without client-supplied layout or image paths.

The persisted Page/LayoutSlot/Panel snapshot is the same Page application
projection read by Page Editor. All candidate images must be registered,
verified READY Artifacts owned by the Panel. Legacy page renderer is reused,
but rendered bytes are registered through the immutable Work ArtifactRepository.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from manga_autopilot.models.panel import PanelLayout
from manga_autopilot.primitives import canonical_json, new_id
from manga_autopilot.repositories import (
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
    PNG_IO_CHUNK_BYTES,
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
    # The actual on-disk integrity check happens *after* the named byte and
    # decoded-pixel budgets are checked, when a verified private input copy is
    # materialized for Pillow. Avoid an unbounded preliminary hash read of a
    # very large (or currently swapped) registered Candidate pathname.
    return artifact


def _copy_verified_candidate_snapshot(
    source: Path, destination: Path, artifact: dict[str, Any],
    *, max_bytes: int, cancel_event: threading.Event | None,
) -> None:
    """Materialize exactly the attested Candidate bytes for Pillow's later open.

    The renderer must never re-open a mutable registered Candidate path.
    The private disposable copy is hash-, length-, format- and size-verified
    before it becomes an input; copying is bounded and uses a pinned regular
    non-symlink descriptor. Failure leaves only TemporaryDirectory-owned bytes.
    """
    expected_bytes = artifact["file_size"]
    if (
        type(expected_bytes) is not int or expected_bytes < 1
        or expected_bytes > max_bytes
    ):
        raise PageExportValidationError(
            f"Panel Candidate {artifact['id']}: stored image file exceeds "
            f"the encoded source input budget of {max_bytes:,} bytes"
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise PageExportValidationError(
            f"Panel Candidate {artifact['id']}: source image is missing or unsafe: {exc}"
        ) from exc
    try:
        with os.fdopen(descriptor, "rb") as stream, destination.open("xb") as output:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise PageExportValidationError(
                    f"Panel Candidate {artifact['id']}: source image is not a regular file"
                )
            digest = hashlib.sha256()
            total = 0
            while chunk := stream.read(PNG_IO_CHUNK_BYTES):
                if cancel_event is not None and cancel_event.is_set():
                    raise PageExportConflictError("Page PNG export cancelled while copying inputs")
                total += len(chunk)
                if total > expected_bytes or total > max_bytes:
                    raise PageExportValidationError(
                        f"Panel Candidate {artifact['id']}: corrupt or modified "
                        "copied input exceeds recorded length or encoded source budget"
                    )
                digest.update(chunk)
                output.write(chunk)
            if total != expected_bytes or digest.hexdigest() != artifact["sha256"]:
                raise PageExportValidationError(
                    f"Panel Candidate {artifact['id']}: corrupt or modified "
                    "copied input hash/size differs from the verified Work Artifact; "
                    "retry export"
                )
    except OSError as exc:
        raise PageExportValidationError(
            f"Panel Candidate {artifact['id']}: cannot copy source image: {exc}"
        ) from exc

    formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}
    try:
        with Image.open(destination) as image:
            if (image.format != formats.get(artifact["mime_type"])
                or image.size != (artifact["width"], artifact["height"])):
                raise PageExportValidationError(
                    f"Panel Candidate {artifact['id']}: copied input image format "
                    "or dimensions differ from the registered Artifact"
                )
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise PageExportValidationError(
            f"Panel Candidate {artifact['id']}: copied source image is corrupt or unsafe"
        ) from exc


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
        cancel_event: threading.Event | None = None,
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
                cancel_event=cancel_event,
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
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        # An aiohttp request can be cancelled while its to_thread() worker
        # continues. Abort before durable publication, rather than silently
        # creating a READY export for an abandoned request. Once a database
        # commit has succeeded it cannot be undone and remains auditable.
        def check_not_cancelled() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise PageExportConflictError(
                    f"Work {work_id}, Page {page_id}: PNG export request was cancelled."
                )

        check_not_cancelled()
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

        # Internal full snapshot is intentional: archived rows participate in
        # revision guards but never in the active compositing inputs.
        state = self.pages.get_page(work_id, page_id, include_archived=True)
        if state["page"]["archived_at"] is not None:
            raise PageExportValidationError(
                f"Page {page_id} is archived; unarchive it before PNG export."
            )
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
        active_panels = [
            panel for panel in state["panels"]
            if panel["archived_at"] is None
        ]
        if not active_panels:
            raise PageExportValidationError(
                f"Page {page_id}: no active Panels available to export; "
                "unarchive at least one Panel before exporting."
            )
        artifact_repo = ArtifactRepository(self.storage_root, work_id)
        work = WorkLifecycleRepository(self.storage_root).open_work(work_id)
        layouts: list[PanelLayout] = []
        input_artifacts: list[dict[str, Any]] = []
        dependencies: list[dict[str, Any]] = []
        total_input_pixels = 0
        total_input_bytes = 0
        for panel in active_panels:
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
            encoded_bytes = artifact["file_size"]
            if type(encoded_bytes) is not int or encoded_bytes < 1:
                raise PageExportValidationError(
                    f"Panel {panel_id}: selected Artifact has invalid stored byte size"
                )
            total_input_bytes += encoded_bytes
            if input_area > budget.max_input_pixels or (
                total_input_pixels > budget.max_total_input_pixels
            ):
                raise PageExportValidationError(
                    f"Work {work_id}, Page {page_id}, Panel {panel_id}: "
                    "registered candidate image input pixel budget exceeded "
                    f"for export_profile {budget.name!r}. Use smaller images "
                    "or the explicit 'print' profile."
                )
            if encoded_bytes > budget.max_input_bytes or (
                total_input_bytes > budget.max_total_input_bytes
            ):
                raise PageExportValidationError(
                    f"Work {work_id}, Page {page_id}, Panel {panel_id}: "
                    "encoded Candidate input budget exceeded for "
                    f"export_profile {budget.name!r}; use smaller images "
                    "or the explicit print profile."
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
            input_artifacts.append(artifact)
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
            for panel in active_panels
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
            # Every image path supplied to Pillow points to bytes already
            # independently pinned and verified from the Candidate Artifact.
            # A local actor changing a registered path during rendering can
            # neither alter these disposable private source snapshots nor
            # substitute pixels for the SHA256 attested in this Work commit.
            pinned_layouts: list[PanelLayout] = []
            for index, (panel_layout, artifact) in enumerate(
                zip(layouts, input_artifacts, strict=True)
            ):
                check_not_cancelled()
                suffix = {
                    "image/png": ".png", "image/jpeg": ".jpg",
                    "image/webp": ".webp",
                }[artifact["mime_type"]]
                verified_path = Path(render_dir) / f"input_{index:06d}{suffix}"
                _copy_verified_candidate_snapshot(
                    Path(panel_layout.image_path), verified_path, artifact,
                    max_bytes=budget.max_input_bytes, cancel_event=cancel_event,
                )
                pinned_layouts.append(replace(panel_layout, image_path=str(verified_path)))
            check_not_cancelled()
            result = render_page_to_png(
                f"page_{state['page']['page_number']}",
                pinned_layouts,
                output_dir=render_dir,
                page_width=width,
                page_height=height,
                background=background,
                outer_border=outer_border,
            )
            # Cancellation may arrive while Pillow is busy. Its disposable
            # TemporaryDirectory is always cleaned before any Artifact write.
            check_not_cancelled()
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
            if _snapshot_fingerprint(self.pages.get_page(work_id, page_id, include_archived=True)) != (
                fingerprint_payload["page_state_sha256"]
            ):
                raise PageExportConflictError(
                    f"Page {page_id} changed while rendering; reload and export again."
                )
            for dependency in dependencies:
                check_not_cancelled()
                artifact_repo.verify_registered_file(dependency["artifact_id"])
            check_not_cancelled()
            # Recheck the EXACT persisted input projection inside the final
            # Artifact registration's BEGIN IMMEDIATE transaction. The earlier
            # post-render read is only a fast-fail optimization: it cannot
            # protect the window while output bytes are copied and published.
            def guard_source_revision(conn):
                # Final cancellation gate shares the BEGIN IMMEDIATE Work lock
                # with provenance/commit validation; an interrupted export
                # cannot create a READY row after this guard sees cancellation.
                check_not_cancelled()
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
                if current["page"]["archived_at"] is not None:
                    raise PageExportConflictError(
                        f"Page {page_id} was archived before PNG commit."
                    )
                # Artifacts are immutable, but candidate publication itself
                # changes implicit selection when no ID was pinned to a Panel.
                # Verify the selected inputs and unique fallback under the
                # same lock, without a nested repository/file-system read.
                for panel, dependency in zip(
                    (p for p in current["panels"] if p["archived_at"] is None),
                    dependencies_by_order, strict=True
                ):
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
            artifact = artifact_repo._register_verified_page_render_file(
                source_path=result.output_path,
                relative_path=relative_path,
                page_id=page_id,
                dependency_fingerprint=fingerprint,
                artifact_id=artifact_id,
                commit_guard=guard_source_revision,
                source_fingerprint_payload=fingerprint_payload,
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
