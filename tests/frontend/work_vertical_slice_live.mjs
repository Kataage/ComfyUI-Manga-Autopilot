/**
 * #242: Execute the *real* Page Editor and Export Center browser modules
 * against a live aiohttp server and its Work SQLite DB.
 *
 * Run twice in independent Node processes: first export, then server
 * restart + reopened editor + another saved edit/export. Python orchestrates
 * the restart and verifies file hashes, pixels, ownership, persistence.
 *
 * This is a small DOM event shim, NOT a graphical ComfyUI browser session.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

const [phase, origin, preferencesPath] = process.argv.slice(2);
assert.ok(["first", "reopen"].includes(phase), "phase must be first or reopen");
assert.ok(origin && preferencesPath, "live server URL and preferences path required");
const WORK = "work_vertical_242";
const PAGE = "page_1";
const PANEL_RED = "panel_red";
const SLOT_RED = "slot_red";
const pngRoot = "/manga_autopilot/api/v2/works/" + WORK;
const preferences = new Map(Object.entries(JSON.parse(
  await readFile(preferencesPath, "utf8")
)));

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.dataset = {};
    this.events = new Map();
    this.value = "";
    this.disabled = false;
    this._text = "";
    this.className = "";
  }
  set textContent(value) {
    this._text = String(value);
    this.replaceChildren();
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...nodes) { for (const node of nodes) this.appendChild(node); }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this.append(...nodes);
  }
  get options() { return this.children.filter((node) => node.tagName === "OPTION"); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, fn) {
    const list = this.events.get(name) || [];
    list.push(fn);
    this.events.set(name, list);
  }
  removeEventListener(name, fn) {
    this.events.set(name, (this.events.get(name) || []).filter((handler) => handler !== fn));
  }
  async fire(name, extra = {}) {
    const ev = {
      clientX: 0, clientY: 0, preventDefault() {},
      ...extra,
    };
    await Promise.all((this.events.get(name) || []).map((fn) => fn(ev)));
  }
  querySelectorAll(tag) {
    const output = [];
    const upper = tag.toUpperCase();
    function visit(node) {
      for (const child of node.children) {
        if (child.tagName === upper) output.push(child);
        visit(child);
      }
    }
    visit(this);
    return output;
  }
  getBoundingClientRect() {
    return { width: 600, height: 800, left: 0, top: 0 };
  }
}

const eventDoc = new Element("document");
globalThis.document = {
  createElement: (tag) => new Element(tag),
  addEventListener: (name, fn) => eventDoc.addEventListener(name, fn),
  removeEventListener: (name, fn) => eventDoc.removeEventListener(name, fn),
  fire: (name, details) => eventDoc.fire(name, details),
};
globalThis.localStorage = {
  getItem: (key) => preferences.get(key) || null,
  setItem: (key, value) => preferences.set(key, String(value)),
};

const rawFetch = globalThis.fetch;
const requests = [];
globalThis.fetch = async (path, options = {}) => {
  assert.equal(typeof path, "string", "all browser requests must use relative paths");
  assert.ok(path.startsWith(pngRoot + "/"), "legacy or unrelated HTTP request: " + path);
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : undefined;
  if (method === "PATCH") {
    assert.ok(path.endsWith("/pages/" + PAGE + "/layout"));
    assert.equal(Object.hasOwn(body, "panels"), false);
    assert.ok(Number.isInteger(body.expected_revision) && body.expected_revision > 0);
  }
  if (method === "POST") {
    assert.equal(path, pngRoot + "/pages/" + PAGE + "/export/png");
    assert.deepEqual(body, {}, "export must not transmit browser layouts or file paths");
  }
  requests.push({ path, method, body });
  return rawFetch(new URL(path, origin), options);
};

async function importBrowserModule(name) {
  const src = await readFile(join(process.cwd(), "web", name), "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src).toString("base64"));
}
const editor = await importBrowserModule("page_editor.js");
const exportCenter = await importBrowserModule("export_center.js");
const find = (root, tag, label) => {
  const item = root.querySelectorAll(tag).find((node) => node.textContent.includes(label));
  assert.ok(item, "missing " + tag + " / " + label);
  return item;
};
async function until(predicate, context) {
  for (let i = 0; i < 300; i++) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw Error("timed out: " + context);
}
async function jsonGet(path) {
  const response = await globalThis.fetch(path);
  assert.equal(response.status, 200, "GET " + path + " status");
  return response.json();
}

const initial = await editor.loadPersistedPage(WORK, PAGE);
assert.equal(initial.work_id, WORK);
assert.equal(initial.page.id, PAGE);
assert.equal(initial.layout.geometry_json.width, phase === "first" ? 300 : 300);
const initialSlot = initial.slots.find((slot) => slot.id === SLOT_RED);
const oldX = phase === "first" ? 20 : 80;
assert.equal(initialSlot.geometry_json.x, oldX);
assert.equal(initial.panels.length, 2);
assert.equal(initial.panels.find((p) => p.id === PANEL_RED).selected_candidate_id, "artifact_red");

const editorRoot = new Element("div");
const disposeEditor = editor.mountPageEditor(editorRoot,
  phase === "first" ? { workId: WORK } : {});
await until(() => editorRoot.textContent.includes("Loaded saved Page"), "editor initial load");
assert.ok(!editorRoot.textContent.includes("panel_01"), "no phantom Panels");
const previousRevision = initial.slots.find((slot) => slot.id === SLOT_RED).revision;
if (phase === "reopen") {
  const width = find(editorRoot, "label", "Page width").querySelectorAll("input")[0];
  width.value = "360";
  await width.fire("change");
}
// Changing page dimensions redraws the slot nodes; drag the *current* node.
const slot = editorRoot.querySelectorAll("div").find((node) => node.dataset.slotId === SLOT_RED);
assert.ok(slot, "real persisted slot available to drag");
await slot.fire("pointerdown", { clientX: 0, clientY: 0 });
await document.fire("pointermove", { clientX: 120, clientY: 0 });
await document.fire("pointerup");
const save = find(editorRoot, "button", "Save layout");
assert.equal(save.disabled, false, "drag should make saved Layout dirty");
await save.fire("click");
assert.match(editorRoot.textContent, /Saved to Work database/);
const saved = await editor.loadPersistedPage(WORK, PAGE);
const savedSlot = saved.slots.find((s) => s.id === SLOT_RED);
const desiredX = phase === "first" ? 80 : 152;
const desiredWidth = phase === "first" ? 300 : 360;
assert.equal(savedSlot.geometry_json.x, desiredX, "real backend slot position");
assert.equal(savedSlot.revision, previousRevision + 1, "revisioned Work DB write");
assert.equal(saved.layout.geometry_json.width, desiredWidth);
assert.equal(saved.panels.find((p) => p.id === PANEL_RED).selected_candidate_id, "artifact_red");
assert.deepEqual(saved.panels.find((p) => p.id === PANEL_RED).generation_spec_json, {
  seed: 42, quality: "draft",
}, "editor must preserve semantic generation fields");
disposeEditor();
assert.equal(editorRoot.children.length, 0);

const exportRoot = new Element("div");
const disposeExport = exportCenter.mountExportCenter(exportRoot);
await until(() => exportRoot.textContent.includes("Loaded 1 saved Pages"), "Export Center reload");
assert.equal(find(exportRoot, "button", "Export PNG").disabled, false);
await find(exportRoot, "button", "Export PNG").fire("click");
assert.match(exportRoot.textContent, /PNG exported and registered/);
const currentExports = await exportCenter.listWorkExports(WORK);
const expectedCount = phase === "first" ? 1 : 2;
assert.equal(currentExports.length, expectedCount, "Work registered export count");
const mostRecent = currentExports[0];
assert.equal(mostRecent.scope_id, PAGE);
assert.equal(mostRecent.artifact_type, "page_render");
assert.equal(mostRecent.width, desiredWidth);
assert.equal(mostRecent.height, 200);
assert.equal(mostRecent.status, "READY");
assert.equal(mostRecent.freshness, "CURRENT");
assert.equal(mostRecent.is_current, true);
assert.match(exportRoot.textContent, /Current · Page/);
if (phase === "reopen") {
  assert.equal(currentExports[1].freshness, "STALE");
  assert.equal(currentExports[1].is_current, false);
  assert.equal(currentExports[1].freshness_reason, "page_inputs_changed");
  assert.ok(exportRoot.textContent.includes("Stale (historical)"));
}
const exportLink = exportRoot.querySelectorAll("a").find((node) =>
  node.href === exportCenter.exportPngFileUrl(WORK, mostRecent.id));
assert.ok(exportLink, "Export Center must link registered Work Artifact ID");
const png = await globalThis.fetch(exportLink.href);
assert.equal(png.status, 200);
assert.equal(png.headers.get("content-type"), "image/png");
const imageBytes = Buffer.from(await png.arrayBuffer());
assert.equal(createHash("sha256").update(imageBytes).digest("hex"), mostRecent.sha256);
assert.equal(imageBytes.byteLength, mostRecent.file_size);
disposeExport();

const reopenedEditor = new Element("div");
const disposeReopenedEditor = editor.mountPageEditor(reopenedEditor);
await until(() => reopenedEditor.textContent.includes("Loaded saved Page"), "editor reopen");
const reopenedSlot = reopenedEditor.querySelectorAll("div").find((node) =>
  node.dataset.slotId === SLOT_RED);
assert.ok(reopenedSlot, "reopen must use real persisted Slot ID");
assert.equal(reopenedSlot.style.left, (100 * desiredX / desiredWidth) + "%");
assert.equal(find(reopenedEditor, "button", "Save layout").disabled, true);
disposeReopenedEditor();

const patchCalls = requests.filter((request) => request.method === "PATCH");
const exportCalls = requests.filter((request) => request.method === "POST");
assert.equal(patchCalls.length, 1, "UI issued exactly one v2 revisioned PATCH");
assert.equal(exportCalls.length, 1, "UI issued exactly one Work PNG POST");
assert.ok(requests.every((request) => !request.path.includes("/api/projects/")));
assert.equal(preferences.get("manga_autopilot_editor_work_id"), WORK);
assert.equal(preferences.get("manga_autopilot_editor_page_id"), PAGE);
await writeFile(preferencesPath, JSON.stringify(Object.fromEntries(preferences)), "utf8");
console.log(JSON.stringify({
  phase, work_id: WORK, page_id: PAGE,
  slot_x: savedSlot.geometry_json.x,
  slot_revision: savedSlot.revision,
  page_width: saved.layout.geometry_json.width,
  artifact_id: mostRecent.id,
  sha256: mostRecent.sha256,
  relative_path: mostRecent.relative_path,
  file_size: mostRecent.file_size,
  dependency_fingerprint: mostRecent.dependency_fingerprint,
  registered_count: expectedCount,
  request_methods: requests.map((r) => r.method),
}));
