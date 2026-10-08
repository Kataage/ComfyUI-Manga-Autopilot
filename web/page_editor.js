/**
 * State-backed Manga Autopilot Page Editor.
 * Work SQLite owns Page, LayoutSlot and Panel identity. This UI never
 * fabricates Panel rows or writes legacy projects/<id>/panels.json.
 */
const ROOT = "/manga_autopilot/api/v2/works";
const STORAGE_WORK = "manga_autopilot_editor_work_id";
const STORAGE_PAGE = "manga_autopilot_editor_page_id";
const DEFAULT_VIEW = Object.freeze({ width: 1200, height: 1600 });

function positiveDimension(value, fallback) {
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

export function pageApiPath(workId, pageId = null) {
  if (typeof workId !== "string" || !workId.trim()) {
    throw new Error("A Work ID is required.");
  }
  let path = ROOT + "/" + encodeURIComponent(workId.trim()) + "/pages";
  if (pageId !== null) {
    if (typeof pageId !== "string" || !pageId.trim()) {
      throw new Error("A Page ID is required.");
    }
    path += "/" + encodeURIComponent(pageId.trim());
  }
  return path;
}

export class PageEditorApiError extends Error {
  constructor(message, status, details = {}) {
    super(message);
    this.name = "PageEditorApiError";
    this.status = status;
    this.details = details;
  }
}

async function jsonRequest(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (err) {
    throw new PageEditorApiError("Network error: " + err.message, 0);
  }
  const content = await response.text();
  let data;
  try {
    data = content ? JSON.parse(content) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const message = (data && (data.message || data.error))
      || content || response.statusText || "Request failed";
    throw new PageEditorApiError(message, response.status, data || {});
  }
  if (!data || typeof data !== "object") {
    throw new PageEditorApiError("Server returned an invalid JSON response.", response.status);
  }
  return data;
}

export async function listPersistedPages(workId) {
  const data = await jsonRequest(pageApiPath(workId));
  if (!Array.isArray(data.pages)) {
    throw new PageEditorApiError("Page list response has no pages.", 200);
  }
  return data.pages;
}

export async function loadPersistedPage(workId, pageId) {
  const data = await jsonRequest(pageApiPath(workId, pageId));
  if (data.work_id !== workId || (data.page && data.page.id) !== pageId
    || !Array.isArray(data.slots) || !Array.isArray(data.panels)) {
    throw new PageEditorApiError("Page response is missing persisted state.", 200);
  }
  return data;
}

/**
 * Build a v2 command from saved revisions only. Never invent IDs or overwrite
 * Panel action, emotion, generation specification, or candidate state.
 */
export function buildLayoutPatch(snapshot, edits = {}) {
  const layout = snapshot && snapshot.layout;
  if (!layout || !Number.isInteger(layout.revision) || layout.revision < 1) {
    throw new Error("A saved Layout and its revision are required.");
  }
  const next = { expected_revision: layout.revision };
  const geometry = edits.geometry;
  if (geometry && JSON.stringify(geometry) !== JSON.stringify(layout.geometry_json)) {
    next.geometry_json = geometry;
  }
  const slots = new Map((snapshot.slots || []).map((slot) => [slot.id, slot]));
  const panels = new Map((snapshot.panels || []).map((panel) => [panel.id, panel]));
  const updates = [];
  for (const [id, shape] of (edits.slots || new Map())) {
    const slot = slots.get(id);
    if (!slot || !Number.isInteger(slot.revision) || slot.revision < 1) {
      throw new Error("Unknown or unversioned LayoutSlot: " + id);
    }
    if (JSON.stringify(shape) !== JSON.stringify(slot.geometry_json)) {
      updates.push({ id: slot.id, expected_revision: slot.revision, geometry_json: shape });
    }
  }
  if (updates.length) next.slot_updates = updates;
  const bindings = [];
  for (const [id, slotId] of (edits.bindings || new Map())) {
    const panel = panels.get(id);
    if (!panel || !Number.isInteger(panel.revision) || panel.revision < 1) {
      throw new Error("Unknown or unversioned Panel: " + id);
    }
    if (slotId !== null && !slots.has(slotId)) {
      throw new Error("Unknown LayoutSlot binding: " + slotId);
    }
    if (panel.layout_slot_id !== slotId) {
      bindings.push({ id: panel.id, expected_revision: panel.revision, layout_slot_id: slotId });
    }
  }
  if (bindings.length) next.panel_bindings = bindings;
  return next;
}

export async function savePersistedLayout(workId, pageId, snapshot, edits = {}) {
  const body = buildLayoutPatch(snapshot, edits);
  if (!("geometry_json" in body) && !body.slot_updates?.length
    && !body.panel_bindings?.length) {
    throw new Error("There are no changes to save.");
  }
  const data = await jsonRequest(pageApiPath(workId, pageId) + "/layout", {
    method: "PATCH", body: JSON.stringify(body),
  });
  if (data.work_id !== workId || (data.page && data.page.id) !== pageId
    || !data.layout || !Array.isArray(data.slots) || !Array.isArray(data.panels)) {
    throw new PageEditorApiError("Save returned an invalid persisted Page.", 200);
  }
  return data;
}

function element(tag, content = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function savedPreference(key) {
  try {
    return globalThis.localStorage?.getItem(key) || "";
  } catch {
    return "";
  }
}

function storePreference(key, value) {
  try {
    globalThis.localStorage?.setItem(key, value);
  } catch {
    // Disabled storage affects only the remembered selection, never the Work.
  }
}

/**
 * Mount a Work-specific Page Editor and return a disposer.
 * Legacy projectId/pageNumber are deliberately ignored; v2 uses Work/Page IDs.
 */
export function mountPageEditor(container, opts = {}) {
  let disposed = false;
  let requestToken = 0;
  let busy = false;
  let snapshot = null;
  let dirty = false;
  let stale = false;
  let layoutGeometry = null;
  let slotEdits = new Map();
  let bindingEdits = new Map();
  let currentPageId = "";
  const documentListeners = [];
  container.replaceChildren();

  const toolbar = element("div", "", "manga-autopilot-editor-toolbar");
  toolbar.style.display = "flex";
  toolbar.style.flexWrap = "wrap";
  toolbar.style.gap = "8px";
  const workLabel = element("label", "Work ID");
  const workInput = document.createElement("input");
  workInput.type = "text";
  workInput.placeholder = "Existing Work ID";
  workInput.value = opts.workId || savedPreference(STORAGE_WORK);
  workLabel.appendChild(workInput);
  toolbar.appendChild(workLabel);
  const loadBtn = element("button", "Load pages");
  loadBtn.type = "button";
  toolbar.appendChild(loadBtn);
  const pageLabel = element("label", "Page");
  const pageSelect = document.createElement("select");
  pageSelect.disabled = true;
  pageLabel.appendChild(pageSelect);
  toolbar.appendChild(pageLabel);
  const saveBtn = element("button", "Save layout");
  saveBtn.type = "button";
  saveBtn.disabled = true;
  toolbar.appendChild(saveBtn);
  const reloadBtn = element("button", "Reload latest (discard edits)");
  reloadBtn.type = "button";
  reloadBtn.disabled = true;
  toolbar.appendChild(reloadBtn);

  const status = element("p", "Enter an existing Work ID and load its Pages.");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const dimensions = element("div", "", "manga-autopilot-editor-dimensions");
  const widthLabel = element("label", "Page width");
  const heightLabel = element("label", "Page height");
  const widthInput = document.createElement("input");
  const heightInput = document.createElement("input");
  for (const input of [widthInput, heightInput]) {
    input.type = "number";
    input.min = "1";
    input.step = "1";
    input.disabled = true;
  }
  widthLabel.appendChild(widthInput);
  heightLabel.appendChild(heightInput);
  dimensions.appendChild(widthLabel);
  dimensions.appendChild(heightLabel);
  const stage = element("div", "", "manga-autopilot-editor-stage");
  stage.style.position = "relative";
  stage.style.width = "100%";
  stage.style.maxWidth = "680px";
  stage.style.aspectRatio = "3 / 4";
  stage.style.background = "var(--comfy-input-bg, #fafafa)";
  stage.style.border = "1px solid var(--border-color, #aaa)";
  stage.style.boxSizing = "border-box";
  stage.style.overflow = "hidden";
  const bindings = element("div", "", "manga-autopilot-editor-bindings");
  stage.appendChild(element("p", "No Page is loaded."));
  for (const el of [toolbar, status, dimensions, stage, bindings]) {
    container.appendChild(el);
  }

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.style.color = isError ? "var(--error-text, #d44)" : "";
  }

  function updateControls() {
    loadBtn.disabled = busy;
    pageSelect.disabled = busy || !pageSelect.options.length;
    saveBtn.disabled = busy || !snapshot?.layout || !dirty || stale;
    reloadBtn.disabled = busy || !currentPageId;
    widthInput.disabled = busy || !snapshot?.layout || stale;
    heightInput.disabled = busy || !snapshot?.layout || stale;
  }

  function markDirty() {
    dirty = true;
    setStatus("Unsaved layout changes.");
    updateControls();
  }

  function resetEdits() {
    slotEdits = new Map();
    bindingEdits = new Map();
    layoutGeometry = snapshot?.layout ? { ...snapshot.layout.geometry_json } : null;
    dirty = false;
    stale = false;
    const size = layoutGeometry || {};
    widthInput.value = String(positiveDimension(size.width, DEFAULT_VIEW.width));
    heightInput.value = String(positiveDimension(size.height, DEFAULT_VIEW.height));
    updateControls();
  }

  function removeDragListeners() {
    for (const [event, handler] of documentListeners) {
      document.removeEventListener(event, handler);
    }
    documentListeners.length = 0;
  }

  function draw() {
    removeDragListeners();
    stage.replaceChildren();
    bindings.replaceChildren();
    if (!snapshot) {
      stage.appendChild(element("p", "No Page is loaded."));
      return;
    }
    if (!snapshot.layout) {
      stage.appendChild(element("p", "This Page has no saved Layout yet."));
      return;
    }
    const size = layoutGeometry || snapshot.layout.geometry_json || {};
    const pageWidth = positiveDimension(Number(size.width), DEFAULT_VIEW.width);
    const pageHeight = positiveDimension(Number(size.height), DEFAULT_VIEW.height);
    stage.style.aspectRatio = pageWidth + " / " + pageHeight;
    const boundPanels = new Map();
    for (const panel of snapshot.panels) {
      const key = bindingEdits.has(panel.id)
        ? bindingEdits.get(panel.id) : panel.layout_slot_id;
      if (key) {
        const names = boundPanels.get(key) || [];
        names.push(panel.id);
        boundPanels.set(key, names);
      }
    }
    if (!snapshot.slots.length) {
      stage.appendChild(element("p", "No persisted LayoutSlots. Nothing to drag."));
    }
    for (const slot of snapshot.slots) {
      const shape = slotEdits.get(slot.id) || slot.geometry_json || {};
      const width = positiveDimension(Number(shape.width), 100);
      const height = positiveDimension(Number(shape.height), 100);
      const x = Number(shape.x) || 0;
      const y = Number(shape.y) || 0;
      const label = (boundPanels.get(slot.id) || []).join(", ")
        || "Unassigned: " + slot.slot_key;
      const box = element("div", label, "manga-autopilot-slot");
      box.dataset.slotId = slot.id;
      box.style.position = "absolute";
      box.style.left = (100 * x / pageWidth) + "%";
      box.style.top = (100 * y / pageHeight) + "%";
      box.style.width = (100 * width / pageWidth) + "%";
      box.style.height = (100 * height / pageHeight) + "%";
      box.style.boxSizing = "border-box";
      box.style.border = "2px solid var(--border-color, #888)";
      box.style.background = "rgba(128, 128, 128, 0.12)";
      box.style.cursor = "move";
      box.style.touchAction = "none";
      stage.appendChild(box);
      let active = false;
      let startX = 0;
      let startY = 0;
      let initialX = x;
      let initialY = y;
      function onMove(event) {
        if (!active || disposed || busy || stale) return;
        const rect = stage.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const nextX = clamp(initialX + (event.clientX - startX) * pageWidth / rect.width,
          0, Math.max(0, pageWidth - width));
        const nextY = clamp(initialY + (event.clientY - startY) * pageHeight / rect.height,
          0, Math.max(0, pageHeight - height));
        box.style.left = (100 * nextX / pageWidth) + "%";
        box.style.top = (100 * nextY / pageHeight) + "%";
        slotEdits.set(slot.id, { ...shape, x: Math.round(nextX), y: Math.round(nextY) });
        markDirty();
      }
      function onUp() { active = false; }
      box.addEventListener("pointerdown", (event) => {
        if (busy || stale) return;
        active = true;
        startX = event.clientX;
        startY = event.clientY;
        initialX = Number((slotEdits.get(slot.id) || shape).x) || 0;
        initialY = Number((slotEdits.get(slot.id) || shape).y) || 0;
        event.preventDefault();
      });
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
      documentListeners.push(["pointermove", onMove], ["pointerup", onUp]);
    }
    if (snapshot.panels.length) {
      bindings.appendChild(element("h4", "Panel to LayoutSlot bindings"));
    }
    for (const panel of snapshot.panels) {
      const label = element("label", panel.id);
      const selector = document.createElement("select");
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "(unassigned)";
      selector.appendChild(none);
      for (const slot of snapshot.slots) {
        const option = document.createElement("option");
        option.value = slot.id;
        option.textContent = slot.slot_key;
        selector.appendChild(option);
      }
      selector.value = bindingEdits.has(panel.id)
        ? (bindingEdits.get(panel.id) || "") : (panel.layout_slot_id || "");
      selector.disabled = busy || stale;
      selector.addEventListener("change", () => {
        bindingEdits.set(panel.id, selector.value || null);
        markDirty();
        draw();
      });
      label.appendChild(selector);
      bindings.appendChild(label);
    }
  }

  function applySnapshot(data) {
    snapshot = data;
    currentPageId = data.page.id;
    storePreference(STORAGE_PAGE, currentPageId);
    resetEdits();
    draw();
    setStatus("Loaded saved Page " + data.page.page_number + " (" + data.page.id + ").");
  }

  async function loadPage(pageId, discard = false) {
    if (!discard && dirty) {
      setStatus("Unsaved edits exist. Save or reload before switching Pages.", true);
      pageSelect.value = currentPageId;
      return;
    }
    const token = ++requestToken;
    busy = true;
    updateControls();
    setStatus("Loading persisted Page...");
    try {
      const data = await loadPersistedPage(workInput.value.trim(), pageId);
      if (disposed || token !== requestToken) return;
      applySnapshot(data);
    } catch (err) {
      if (disposed || token !== requestToken) return;
      snapshot = null;
      currentPageId = "";
      resetEdits();
      draw();
      setStatus("Load failed: " + err.message, true);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  }

  async function loadWorkPages() {
    if (dirty) {
      setStatus("Unsaved edits exist. Save or reload before changing Works.", true);
      return;
    }
    const token = ++requestToken;
    busy = true;
    snapshot = null;
    currentPageId = "";
    pageSelect.replaceChildren();
    updateControls();
    draw();
    setStatus("Loading Work Pages...");
    try {
      const workId = workInput.value.trim();
      const pages = await listPersistedPages(workId);
      if (disposed || token !== requestToken) return;
      storePreference(STORAGE_WORK, workId);
      for (const page of pages) {
        const option = document.createElement("option");
        option.value = page.id;
        option.textContent = "Page " + page.page_number + " (" + page.id + ")";
        pageSelect.appendChild(option);
      }
      if (!pages.length) {
        setStatus("This Work has no persisted Pages. No placeholders were created.");
        return;
      }
      const preferred = opts.pageId || savedPreference(STORAGE_PAGE);
      const target = pages.some((page) => page.id === preferred)
        ? preferred : pages[0].id;
      pageSelect.value = target;
      const data = await loadPersistedPage(workId, target);
      if (disposed || token !== requestToken) return;
      applySnapshot(data);
    } catch (err) {
      if (disposed || token !== requestToken) return;
      pageSelect.replaceChildren();
      setStatus("Load failed: " + err.message, true);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  }

  function editDimension(field, raw) {
    if (!snapshot?.layout || busy || stale) return;
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < 1) {
      setStatus(field + " must be a positive integer.", true);
      return;
    }
    layoutGeometry = { ...layoutGeometry, [field]: value };
    markDirty();
    draw();
  }

  widthInput.addEventListener("change", () => editDimension("width", widthInput.value));
  heightInput.addEventListener("change", () => editDimension("height", heightInput.value));
  loadBtn.addEventListener("click", loadWorkPages);
  pageSelect.addEventListener("change", () => loadPage(pageSelect.value));
  reloadBtn.addEventListener("click", () => {
    if (currentPageId) loadPage(currentPageId, true);
  });
  saveBtn.addEventListener("click", async () => {
    if (!dirty || busy || stale || !snapshot?.layout) return;
    const token = ++requestToken;
    busy = true;
    updateControls();
    setStatus("Saving layout...");
    try {
      const data = await savePersistedLayout(
        workInput.value.trim(), currentPageId, snapshot,
        { geometry: layoutGeometry, slots: slotEdits, bindings: bindingEdits }
      );
      if (disposed || token !== requestToken) return;
      applySnapshot(data);
      setStatus("Saved to Work database.");
    } catch (err) {
      if (disposed || token !== requestToken) return;
      stale = err instanceof PageEditorApiError && err.status === 409;
      const isRevision = stale && err.details.error === "revision_conflict";
      setStatus(isRevision
        ? "Revision conflict: " + err.message + ". Reload latest before editing again."
        : "Save failed: " + err.message, true);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  });

  updateControls();
  if (workInput.value.trim()) {
    void loadWorkPages();
  }
  return () => {
    disposed = true;
    ++requestToken;
    removeDragListeners();
    container.replaceChildren();
  };
}

export const __test__ = { clamp, positiveDimension };
