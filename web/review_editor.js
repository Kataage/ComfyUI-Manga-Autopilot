/**
 * Manga Autopilot review editor (spec section 22, plan Task 8).
 *
 * Shows where a strict Anima run is blocked, lets the user edit the planned
 * fields as a form, and approves or rejects the gate that is holding the run.
 *
 * Deliberately NOT here: free-drag layout editing, undo/redo, and image diff.
 * Panel geometry belongs to `page_editor.js`; this view is about deciding.
 *
 * The pure helpers below carry no DOM or network dependency so they can be
 * exercised directly; `mountReviewEditor` is the only part that needs a browser.
 */

export const GATES = Object.freeze([
  "story",
  "storyboard",
  "artwork_early",
  "artwork_final",
]);

export const GATE_LABELS = Object.freeze({
  story: "Story",
  storyboard: "Storyboard",
  artwork_early: "Artwork (first page)",
  artwork_final: "Artwork (final)",
});

const STATUS_LABELS = Object.freeze({
  pending: "Not started",
  awaiting_review: "Waiting for you",
  approved: "Approved",
  rejected: "Rejected",
});

/** Fields offered per gate. Editing is form-based on purpose: no canvas here. */
export const GATE_FIELDS = Object.freeze({
  story: [
    { key: "title", label: "Title", type: "text" },
    { key: "logline", label: "Logline", type: "textarea" },
    { key: "theme", label: "Theme", type: "text" },
  ],
  storyboard: [
    { key: "purpose", label: "Panel purpose", type: "text" },
    { key: "shot", label: "Shot", type: "text" },
    { key: "camera_angle", label: "Camera angle", type: "text" },
    { key: "layout_id", label: "Layout", type: "select" },
  ],
  artwork_early: [
    { key: "positive", label: "Positive prompt", type: "textarea" },
    { key: "seed", label: "Seed", type: "number" },
  ],
  artwork_final: [
    { key: "positive", label: "Positive prompt", type: "textarea" },
    { key: "seed", label: "Seed", type: "number" },
    { key: "dialogue", label: "Dialogue", type: "textarea" },
  ],
});

export function gateLabel(gate) {
  return GATE_LABELS[gate] || gate;
}

export function statusLabel(status) {
  return STATUS_LABELS[status] || status;
}

/** URL helpers. Kept in one place so the routes are asserted in one place. */
export function reviewsUrl(projectId) {
  return `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/reviews`;
}

export function decisionUrl(projectId, gate, decision) {
  return `${reviewsUrl(projectId)}/${encodeURIComponent(gate)}/${decision}`;
}

/**
 * The gate currently holding the run: the first enabled gate not yet approved.
 * Returns null for a project with no gates, or one that has cleared them all.
 */
export function blockingGate(board) {
  const gates = board?.policy?.gates || [];
  for (const gate of gates) {
    if ((board.gates?.[gate]?.status || "pending") !== "approved") return gate;
  }
  return null;
}

/** Whether a decision button should be offered for `gate`. */
export function canDecide(board, gate) {
  const gates = board?.policy?.gates || [];
  if (!gates.includes(gate)) return false;
  return gate === blockingGate(board);
}

/** A flat, render-ready view of the board. */
export function summariseBoard(board) {
  const blocking = blockingGate(board);
  const gates = (board?.policy?.gates || []).map((gate) => {
    const state = board.gates?.[gate] || { status: "pending", decisions: [] };
    return {
      gate,
      label: gateLabel(gate),
      status: state.status || "pending",
      statusLabel: statusLabel(state.status || "pending"),
      isBlocking: gate === blocking,
      note: state.decisions?.length
        ? state.decisions[state.decisions.length - 1].note || ""
        : "",
    };
  });
  return { gates, blocking, complete: gates.length > 0 && blocking === null };
}

/**
 * Stages an edit invalidated, mirrored from the backend's edit_invalidation.
 * Used only to draw the "stale" markers; the backend remains authoritative.
 */
export const STALE_STAGES = Object.freeze({
  dialogue: ["bubbles", "page_render", "exports"],
  image_only: ["panel_images", "page_render", "exports"],
  layout: ["panel_images", "bubbles", "page_render", "exports"],
  continuity: ["panel_images", "bubbles", "page_render", "exports"],
  character: ["panel_images", "page_render", "exports"],
});

export function staleStagesFor(editKind) {
  return STALE_STAGES[editKind] || [];
}

/** A panel is stale when it was invalidated and has not been regenerated. */
export function isStale(record) {
  if (!record) return false;
  if (record.status !== "draft") return false;
  const history = record.history || [];
  return history.some((entry) => entry.kind === "invalidated");
}

export function staleLabel(record) {
  return isStale(record) ? "stale - needs regeneration" : "";
}

async function readJson(response) {
  const text = await response.text();
  if (!response.ok) throw new Error(text || `HTTP ${response.status}`);
  return text ? JSON.parse(text) : {};
}

export async function fetchBoard(projectId, fetchImpl = fetch) {
  return readJson(await fetchImpl(reviewsUrl(projectId)));
}

export async function submitDecision(projectId, gate, decision, note, fetchImpl = fetch) {
  const response = await fetchImpl(decisionUrl(projectId, gate, decision), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note: note || "" }),
  });
  return readJson(response);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function buildField(field, value, onChange) {
  const wrapper = el("label", "manga-autopilot-field");
  wrapper.appendChild(el("span", "manga-autopilot-field-label", field.label));
  const input =
    field.type === "textarea"
      ? document.createElement("textarea")
      : document.createElement("input");
  if (field.type !== "textarea") input.type = field.type === "number" ? "number" : "text";
  input.value = value === undefined || value === null ? "" : String(value);
  input.dataset.fieldKey = field.key;
  input.addEventListener("change", () => onChange(field.key, input.value));
  wrapper.appendChild(input);
  return wrapper;
}

/**
 * Render the review panel into `container`.
 *
 * `opts.projectId` is required. `opts.fetchImpl` and `opts.values` exist so the
 * view can be driven without a live backend. Returns a teardown function.
 */
export function mountReviewEditor(container, opts = {}) {
  const projectId = opts.projectId;
  const fetchImpl = opts.fetchImpl || fetch;
  const edits = { ...(opts.values || {}) };

  const root = el("div", "manga-autopilot-review");
  const statusList = el("ul", "manga-autopilot-review-gates");
  const form = el("div", "manga-autopilot-review-form");
  const actions = el("div", "manga-autopilot-review-actions");
  const message = el("p", "manga-autopilot-review-message", "");

  const noteInput = document.createElement("textarea");
  noteInput.className = "manga-autopilot-review-note";
  noteInput.placeholder = "Why? (optional, stored with the decision)";

  async function refresh() {
    let board;
    try {
      board = await fetchBoard(projectId, fetchImpl);
    } catch (error) {
      message.textContent = `Could not load reviews: ${error.message}`;
      return;
    }
    const summary = summariseBoard(board);

    statusList.innerHTML = "";
    for (const gate of summary.gates) {
      const item = el("li", "manga-autopilot-review-gate");
      item.dataset.gate = gate.gate;
      item.dataset.status = gate.status;
      item.textContent = `${gate.label}: ${gate.statusLabel}`;
      if (gate.isBlocking) item.classList.add("is-blocking");
      if (gate.note) item.title = gate.note;
      statusList.appendChild(item);
    }

    form.innerHTML = "";
    actions.innerHTML = "";
    if (summary.blocking === null) {
      message.textContent = summary.gates.length
        ? "Every review is approved."
        : "This project has no review gates.";
      return;
    }

    message.textContent = `Waiting on: ${gateLabel(summary.blocking)}`;
    for (const field of GATE_FIELDS[summary.blocking] || []) {
      form.appendChild(
        buildField(field, edits[field.key], (key, value) => {
          edits[key] = value;
        }),
      );
    }

    for (const decision of ["approve", "reject"]) {
      const button = el("button", `manga-autopilot-review-${decision}`, decision);
      button.dataset.decision = decision;
      button.onclick = async () => {
        button.disabled = true;
        try {
          await submitDecision(
            projectId,
            summary.blocking,
            decision,
            noteInput.value,
            fetchImpl,
          );
          noteInput.value = "";
          await refresh();
        } catch (error) {
          message.textContent = `Decision failed: ${error.message}`;
          button.disabled = false;
        }
      };
      actions.appendChild(button);
    }
  }

  root.appendChild(el("h3", "manga-autopilot-review-title", "Reviews"));
  root.appendChild(statusList);
  root.appendChild(message);
  root.appendChild(form);
  root.appendChild(noteInput);
  root.appendChild(actions);
  container.appendChild(root);

  const ready = refresh();

  const teardown = () => {
    container.innerHTML = "";
  };
  teardown.ready = ready;
  teardown.refresh = refresh;
  return teardown;
}

export const __test__ = { buildField, readJson, STATUS_LABELS };
