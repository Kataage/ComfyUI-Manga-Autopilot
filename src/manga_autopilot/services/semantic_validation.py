from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SemanticIssue:
    path: str
    code: str
    message: str
    severity: str = "error"
    fallback: str | None = None


def validate_page_sequence(
    pages: Sequence[Any],
    expected_count: int,
) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    if len(pages) != expected_count:
        issues.append(
            SemanticIssue(
                path="/pages",
                code="page_count",
                message=f"expected {expected_count} pages, received {len(pages)}",
            )
        )
    numbers = [_read(item, "pageNumber", "page_number") for item in pages]
    expected_numbers = list(range(1, len(pages) + 1))
    if numbers != expected_numbers:
        issues.append(
            SemanticIssue(
                path="/pages",
                code="page_number_sequence",
                message=f"page numbers must be {expected_numbers}, received {numbers}",
            )
        )
    return issues


def validate_panel_sequence(
    panels: Sequence[Any],
    expected_count: int,
    character_ids: set[str],
    *,
    layout_id: str | None = None,
    registered_layout_ids: set[str] | None = None,
) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    if len(panels) != expected_count:
        issues.append(
            SemanticIssue(
                path="/panels",
                code="panel_count",
                message=f"expected {expected_count} panels, received {len(panels)}",
            )
        )
    numbers = [_read(item, "panelNumber", "panel_number") for item in panels]
    expected_numbers = list(range(1, len(panels) + 1))
    if numbers != expected_numbers:
        issues.append(
            SemanticIssue(
                path="/panels",
                code="panel_number_sequence",
                message=f"panel numbers must be {expected_numbers}, received {numbers}",
            )
        )
    if layout_id and registered_layout_ids is not None and layout_id not in registered_layout_ids:
        issues.append(
            SemanticIssue(
                path="/layoutId",
                code="unknown_layout",
                message=f"layout {layout_id!r} is not registered",
            )
        )
    for panel_index, panel in enumerate(panels):
        characters = _read(panel, "characters") or []
        for character_index, character_id in enumerate(characters):
            if character_id not in character_ids:
                issues.append(
                    SemanticIssue(
                        path=f"/panels/{panel_index}/characters/{character_index}",
                        code="unknown_character",
                        message=f"character {character_id!r} is not defined",
                    )
                )
    return issues


def validate_page_layouts(
    pages: Sequence[Any],
    layout_slots: Mapping[str, int],
) -> list[SemanticIssue]:
    issues: list[SemanticIssue] = []
    for index, page in enumerate(pages):
        layout_id = _read(page, "layoutId", "layout_id")
        panel_count = int(_read(page, "panelCount", "panel_count") or 0)
        if not layout_id:
            issues.append(
                SemanticIssue(
                    path=f"/pages/{index}/layoutId",
                    code="layout_fallback",
                    message=f"no layout supplied; using fallback_grid_{panel_count}",
                    severity="warning",
                    fallback=f"fallback_grid_{panel_count}",
                )
            )
            continue
        if layout_id not in layout_slots:
            issues.append(
                SemanticIssue(
                    path=f"/pages/{index}/layoutId",
                    code="unknown_layout",
                    message=f"layout {layout_id!r} is not registered",
                )
            )
            continue
        if layout_slots[layout_id] != panel_count:
            issues.append(
                SemanticIssue(
                    path=f"/pages/{index}/panelCount",
                    code="layout_slot_mismatch",
                    message=(
                        f"layout {layout_id!r} has {layout_slots[layout_id]} slots, "
                        f"received panelCount={panel_count}"
                    ),
                )
            )
    return issues


def _read(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


__all__ = [
    "SemanticIssue",
    "validate_page_sequence",
    "validate_page_layouts",
    "validate_panel_sequence",
]
