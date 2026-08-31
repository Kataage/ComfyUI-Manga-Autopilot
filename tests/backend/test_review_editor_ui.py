"""Contract tests for the review editor front-end module (plan Task 8, step 1).

Where a JS runtime is available the pure helpers are executed for real through
Node; those tests skip when Node is absent so CI without a Node step still runs
the rest. The remaining checks assert the module's contract surface - the routes
it calls and the features it deliberately does not implement - which is what the
backend and the plan actually pin down.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
REVIEW_EDITOR = WEB_DIR / "review_editor.js"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


def _source() -> str:
    return REVIEW_EDITOR.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """Strip comments so a scan reads the code, not the prose describing it."""
    import re

    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//.*", "", without_blocks)


def _run_node(body: str, tmp_path: Path) -> object:
    """Import the module in Node, run `body`, and return what it prints as JSON."""
    driver = tmp_path / "driver.mjs"
    module_url = REVIEW_EDITOR.resolve().as_uri()
    driver.write_text(
        f'import * as mod from "{module_url}";\n'
        f"const result = await (async () => {{\n{body}\n}})();\n"
        "process.stdout.write(JSON.stringify(result));\n",
        encoding="utf-8",
    )
    completed = subprocess.run(  # noqa: S603 - fixed executable, no shell
        [NODE, str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


# ------------------------------------------------------------ module exists


def test_review_editor_module_is_shipped() -> None:
    import importlib

    pkg = importlib.import_module("manga_autopilot")
    assert (Path(pkg.WEB_DIRECTORY) / "review_editor.js").exists()


def test_index_mounts_the_review_editor() -> None:
    index = (WEB_DIR / "index.js").read_text(encoding="utf-8")

    assert "review_editor.js" in index
    assert "mountReviewEditor" in index


# ------------------------------------------------------------- pure helpers


@requires_node
def test_blocking_gate_is_the_first_unapproved_gate(tmp_path: Path) -> None:
    result = _run_node(
        """
        const board = {
          policy: { gates: ["story", "storyboard", "artwork_early", "artwork_final"] },
          gates: {
            story: { status: "approved", decisions: [] },
            storyboard: { status: "awaiting_review", decisions: [] },
            artwork_early: { status: "pending", decisions: [] },
            artwork_final: { status: "pending", decisions: [] },
          },
        };
        return mod.blockingGate(board);
        """,
        tmp_path,
    )

    assert result == "storyboard"


@requires_node
def test_a_project_without_gates_is_never_blocking(tmp_path: Path) -> None:
    result = _run_node(
        'return mod.blockingGate({ policy: { gates: [] }, gates: {} });',
        tmp_path,
    )

    assert result is None


@requires_node
def test_only_the_blocking_gate_is_decidable(tmp_path: Path) -> None:
    result = _run_node(
        """
        const board = {
          policy: { gates: ["story", "storyboard"] },
          gates: { story: { status: "approved" }, storyboard: { status: "pending" } },
        };
        return {
          story: mod.canDecide(board, "story"),
          storyboard: mod.canDecide(board, "storyboard"),
          unknown: mod.canDecide(board, "colouring"),
        };
        """,
        tmp_path,
    )

    assert result == {"story": False, "storyboard": True, "unknown": False}


@requires_node
def test_board_summary_marks_the_blocking_gate_and_keeps_the_last_note(
    tmp_path: Path,
) -> None:
    result = _run_node(
        """
        const board = {
          policy: { gates: ["story", "storyboard"] },
          gates: {
            story: { status: "approved", decisions: [{ decision: "approved", note: "ok" }] },
            storyboard: {
              status: "rejected",
              decisions: [
                { decision: "approved", note: "first pass" },
                { decision: "rejected", note: "panel 3 is unreadable" },
              ],
            },
          },
        };
        return mod.summariseBoard(board);
        """,
        tmp_path,
    )

    assert result["blocking"] == "storyboard"
    assert result["complete"] is False
    assert [g["gate"] for g in result["gates"]] == ["story", "storyboard"]
    assert result["gates"][1]["note"] == "panel 3 is unreadable"
    assert result["gates"][1]["isBlocking"] is True
    assert result["gates"][0]["statusLabel"] == "Approved"


@requires_node
def test_a_fully_approved_board_reports_complete(tmp_path: Path) -> None:
    result = _run_node(
        """
        const board = {
          policy: { gates: ["story"] },
          gates: { story: { status: "approved", decisions: [] } },
        };
        return mod.summariseBoard(board);
        """,
        tmp_path,
    )

    assert result["complete"] is True
    assert result["blocking"] is None


@requires_node
def test_urls_match_the_backend_routes(tmp_path: Path) -> None:
    result = _run_node(
        """
        return {
          board: mod.reviewsUrl("proj 1"),
          approve: mod.decisionUrl("proj-1", "artwork_final", "approve"),
          reject: mod.decisionUrl("proj-1", "story", "reject"),
        };
        """,
        tmp_path,
    )

    assert result["board"] == "/manga_autopilot/api/projects/proj%201/reviews"
    assert (
        result["approve"]
        == "/manga_autopilot/api/projects/proj-1/reviews/artwork_final/approve"
    )
    assert result["reject"] == "/manga_autopilot/api/projects/proj-1/reviews/story/reject"


@requires_node
def test_stale_markers_follow_the_backend_rules(tmp_path: Path) -> None:
    result = _run_node(
        """
        return {
          dialogue: mod.staleStagesFor("dialogue"),
          image_only: mod.staleStagesFor("image_only"),
          unknown: mod.staleStagesFor("colouring"),
          staleDraft: mod.isStale({ status: "draft", history: [{ kind: "invalidated" }] }),
          freshDraft: mod.isStale({ status: "draft", history: [] }),
          generated: mod.isStale({ status: "generated", history: [{ kind: "invalidated" }] }),
          label: mod.staleLabel({ status: "draft", history: [{ kind: "invalidated" }] }),
        };
        """,
        tmp_path,
    )

    assert result["dialogue"] == ["bubbles", "page_render", "exports"]
    assert "panel_images" not in result["dialogue"]
    assert result["image_only"] == ["panel_images", "page_render", "exports"]
    assert result["unknown"] == []
    assert result["staleDraft"] is True
    assert result["freshDraft"] is False
    assert result["generated"] is False
    assert "stale" in result["label"]


@requires_node
def test_decision_submission_posts_the_note(tmp_path: Path) -> None:
    result = _run_node(
        """
        const calls = [];
        const fakeFetch = async (url, init) => {
          calls.push({ url, method: init.method, body: JSON.parse(init.body) });
          return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }) };
        };
        const body = await mod.submitDecision(
          "proj-1", "story", "approve", "reads well", fakeFetch,
        );
        return { calls, body };
        """,
        tmp_path,
    )

    assert result["calls"][0]["method"] == "POST"
    assert result["calls"][0]["url"].endswith("/reviews/story/approve")
    assert result["calls"][0]["body"] == {"note": "reads well"}
    assert result["body"] == {"ok": True}


@requires_node
def test_a_failed_request_surfaces_the_server_message(tmp_path: Path) -> None:
    result = _run_node(
        """
        const fakeFetch = async () => ({
          ok: false, status: 404, text: async () => "unknown review gate: colouring",
        });
        try {
          await mod.fetchBoard("proj-1", fakeFetch);
          return "no error";
        } catch (error) {
          return error.message;
        }
        """,
        tmp_path,
    )

    assert result == "unknown review gate: colouring"


@requires_node
def test_gate_names_match_the_backend(tmp_path: Path) -> None:
    from manga_autopilot.services.review_gate import REVIEW_GATES

    result = _run_node("return mod.GATES;", tmp_path)

    assert result == list(REVIEW_GATES)


# ------------------------------------------------------- deliberate omissions


def test_the_review_editor_does_not_reimplement_canvas_editing() -> None:
    """Panel geometry belongs to page_editor.js; undo/redo and diff are out of scope."""
    code = _code_only(_source())

    for excluded in ("mousemove", "getBoundingClientRect", "undo", "redo", "diff"):
        assert excluded not in code, f"review_editor.js should not implement {excluded}"


def test_the_review_editor_only_talks_to_the_review_routes() -> None:
    code = _code_only(_source())

    assert code.count("/manga_autopilot/api/projects/") == 1, (
        "every URL should be built from reviewsUrl so the routes stay in one place"
    )
    assert "method: \"POST\"" in code
    assert "DELETE" not in code


# ------------------------------------------------ accessible name of a decided gate
#
# Driving the module in a browser showed a decided gate announcing only its
# note: `title` becomes the accessible name and hid "Story: Approved" from a
# screen reader, which heard just "premise reads fine".


def test_a_decided_gate_keeps_its_name_in_the_accessible_label() -> None:
    code = _code_only(_source())

    assert "aria-label" in code
    # The label carries the summary and the note, not the note alone.
    assert "${summary}. ${gate.note}" in code


def test_the_summary_is_still_the_visible_text() -> None:
    code = _code_only(_source())

    assert "item.textContent = summary" in code
