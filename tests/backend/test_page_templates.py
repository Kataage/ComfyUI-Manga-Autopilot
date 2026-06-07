"""Tests for the page / webtoon template registry."""

from __future__ import annotations

import pytest

from manga_autopilot.services.page_templates import (
    ALL_TEMPLATES,
    PAGE_TEMPLATES,
    WEBTOON_TEMPLATES,
    get_template,
    list_templates,
)


def test_all_templates_have_unique_ids() -> None:
    ids = [t.template_id for t in ALL_TEMPLATES.values()]
    assert len(ids) == len(set(ids))


def test_list_templates_by_kind() -> None:
    assert {t.template_id for t in list_templates("page")} >= {
        "page_2_horizontal",
        "page_3_t",
        "page_4_grid",
        "page_5_cinematic",
    }
    assert {t.template_id for t in list_templates("webtoon")} >= {
        "webtoon_vertical_3",
        "webtoon_long_strip",
    }


def test_list_templates_all() -> None:
    assert len(list_templates()) == len(PAGE_TEMPLATES) + len(WEBTOON_TEMPLATES)


def test_list_templates_unknown_kind() -> None:
    with pytest.raises(ValueError):
        list_templates("nope")  # type: ignore[arg-type]


def test_get_template_returns_panel_layouts() -> None:
    template = get_template("page_4_grid")
    layouts = template.to_panel_layouts()
    assert len(layouts) == 4
    for layout in layouts:
        assert layout.width > 0
        assert layout.height > 0


def test_get_template_missing() -> None:
    with pytest.raises(KeyError):
        get_template("nope")


def test_webtoon_template_dimensions() -> None:
    wt = get_template("webtoon_long_strip")
    assert wt.page_height > wt.page_width
    assert wt.kind == "webtoon"
