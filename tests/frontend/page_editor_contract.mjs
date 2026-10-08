// Native Node browser-shim tests for Page Editor request and UI contracts.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";

const source = await readFile(join(process.cwd(), "web", "page_editor.js"), "utf8");
// This file is an ESM browser extension even when Node's package default is CJS.
const editor = await import("data:text/javascript;base64,"
  + Buffer.from(source).toString("base64"));

function persisted() {
  return {
    work_id: "work_123",
    page: { id: "page_1", page_number: 1, revision: 2 },
    layout: { id: "layout_1", revision: 3, geometry_json: { width: 1200, height: 1600 } },
    slots: [
      { id: "slot_1", slot_key: "top", revision: 4,
        geometry_json: { x: 10, y: 20, width: 300, height: 200 } },
      { id: "slot_2", slot_key: "bottom", revision: 1,
        geometry_json: { x: 50, y: 400, width: 300, height: 300 } },
    ],
    panels: [
      { id: "panel_persisted", revision: 7, page_id: "page_1",
        layout_slot_id: "slot_1", action_json: { protagonist: "hero" },
        generation_spec_json: { seed: 42 }, panel_purpose: "Reveal" },
    ],
  };
}

function response(status, value) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "HTTP " + status,
    async text() { return JSON.stringify(value); },
  };
}

test("v2 URL encoding and expected-revision request construction", async () => {
  assert.equal(editor.pageApiPath("work/a", "page b"),
    "/manga_autopilot/api/v2/works/work%2Fa/pages/page%20b");
  assert.throws(() => editor.pageApiPath(" "), /Work ID/);
  const snapshot = persisted();
  const original = structuredClone(snapshot);
  const commands = editor.buildLayoutPatch(snapshot, {
    geometry: { width: 1400, height: 1600 },
    slots: new Map([["slot_1", { x: 80, y: 20, width: 300, height: 200 }]]),
    bindings: new Map([["panel_persisted", "slot_2"]]),
  });
  assert.deepEqual(commands, {
    expected_revision: 3,
    geometry_json: { width: 1400, height: 1600 },
    slot_updates: [
      { id: "slot_1", expected_revision: 4,
        geometry_json: { x: 80, y: 20, width: 300, height: 200 } },
    ],
    panel_bindings: [
      { id: "panel_persisted", expected_revision: 7, layout_slot_id: "slot_2" },
    ],
  });
  assert.deepEqual(snapshot, original);
  assert.ok(!("panels" in commands));
  assert.ok(!("page_number" in commands));
  assert.throws(() => editor.buildLayoutPatch(snapshot, {
    slots: new Map([["invented_slot", {}]]),
  }), /Unknown/);
  assert.throws(() => editor.buildLayoutPatch(snapshot, {
    bindings: new Map([["invented_panel", "slot_1"]]),
  }), /Unknown/);
});

test("GET, PATCH, save response, and HTTP 409 preserve the API contract", async () => {
  const old = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (!options.method) {
      if (url.endsWith("/pages")) return response(200, { pages: [persisted().page] });
      return response(200, persisted());
    }
    const body = JSON.parse(options.body);
    assert.equal(options.method, "PATCH");
    assert.equal(body.expected_revision, 3);
    assert.deepEqual(body.slot_updates, [
      { id: "slot_1", expected_revision: 4,
        geometry_json: { x: 90, y: 20, width: 300, height: 200 } },
    ]);
    return response(200, {
      ...persisted(), slots: [{ ...persisted().slots[0],
        revision: 5, geometry_json: body.slot_updates[0].geometry_json },
        persisted().slots[1]],
    });
  };
  try {
    const pages = await editor.listPersistedPages("work_123");
    assert.deepEqual(pages, [persisted().page]);
    const loaded = await editor.loadPersistedPage("work_123", "page_1");
    assert.deepEqual(loaded, persisted());
    const saved = await editor.savePersistedLayout(
      "work_123", "page_1", loaded,
      { slots: new Map([["slot_1", { x: 90, y: 20, width: 300, height: 200 }]]) }
    );
    assert.equal(saved.slots[0].revision, 5);
    assert.equal(requests[2].url,
      "/manga_autopilot/api/v2/works/work_123/pages/page_1/layout");

    globalThis.fetch = async () => response(409, {
      error: "revision_conflict", message: "stale revision",
      entity_id: "layout_1", expected_revision: 3, actual_revision: 4,
    });
    await assert.rejects(
      editor.savePersistedLayout("work_123", "page_1", loaded, {
        geometry: { width: 1300, height: 1600 },
      }),
      (err) => err instanceof editor.PageEditorApiError
        && err.status === 409 && err.details.actual_revision === 4
    );
    globalThis.fetch = async () => response(500, { message: "database failed" });
    await assert.rejects(
      editor.savePersistedLayout("work_123", "page_1", loaded, {
        geometry: { width: 1300, height: 1600 },
      }),
      (err) => err.status === 500 && /database failed/.test(err.message)
    );
  } finally {
    globalThis.fetch = old;
  }
});

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.dataset = {};
    this.events = new Map();
    this._text = "";
    this.value = "";
    this.disabled = false;
    this.className = "";
  }
  set textContent(value) {
    this._text = String(value);
    this.replaceChildren();
  }
  get textContent() {
    return this._text + this.children.map((x) => x.textContent).join("");
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    for (const child of children) this.appendChild(child);
  }
  get options() { return this.children.filter((child) => child.tagName === "OPTION"); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(event, handler) {
    const list = this.events.get(event) || [];
    list.push(handler);
    this.events.set(event, list);
  }
  removeEventListener(event, handler) {
    this.events.set(event, (this.events.get(event) || []).filter((x) => x !== handler));
  }
  async fire(event, details = {}) {
    await Promise.all((this.events.get(event) || []).map((fn) => fn({
      clientX: 0, clientY: 0, preventDefault() {}, ...details,
    })));
  }
  querySelectorAll(selector) {
    const target = selector.toUpperCase();
    const nodes = [];
    function visit(parent) {
      for (const child of parent.children) {
        if (child.tagName === target) nodes.push(child);
        visit(child);
      }
    }
    visit(this);
    return nodes;
  }
  getBoundingClientRect() {
    return { width: 600, height: 800, left: 0, top: 0 };
  }
}

function setupBrowser() {
  const old = {
    fetch: globalThis.fetch, document: globalThis.document,
    localStorage: globalThis.localStorage,
  };
  const prefs = new Map();
  globalThis.document = {
    createElement: (tag) => new FakeElement(tag),
    _events: new FakeElement("document"),
    addEventListener(event, handler) { this._events.addEventListener(event, handler); },
    removeEventListener(event, handler) { this._events.removeEventListener(event, handler); },
    fire(event, data) { return this._events.fire(event, data); },
  };
  globalThis.localStorage = {
    getItem(key) { return prefs.get(key) || null; },
    setItem(key, value) { prefs.set(key, String(value)); },
  };
  return {
    restore() {
      globalThis.fetch = old.fetch;
      globalThis.document = old.document;
      globalThis.localStorage = old.localStorage;
    },
    root() { return new FakeElement("div"); },
  };
}

function find(root, tag, text) {
  return root.querySelectorAll(tag).find((e) => e.textContent.includes(text));
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 5));
}

test("mounted editor edits persisted slot, saves, and reopens without placeholders", async () => {
  const env = setupBrowser();
  let server = persisted();
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/pages")) return response(200, { pages: [server.page] });
    if (options.method === "PATCH") {
      const body = JSON.parse(options.body);
      assert.equal(body.expected_revision, 3);
      assert.equal(body.slot_updates[0].expected_revision, 4);
      server = { ...server, slots: server.slots.map((slot) =>
        slot.id === "slot_1"
          ? { ...slot, revision: 5, geometry_json: body.slot_updates[0].geometry_json }
          : slot),
      };
      return response(200, server);
    }
    return response(200, server);
  };
  try {
    const root = env.root();
    const dispose = editor.mountPageEditor(root, { workId: "work_123" });
    await flush();
    assert.match(root.textContent, /Loaded saved Page/);
    assert.ok(!root.textContent.includes("panel_01"));
    const saveButton = find(root, "button", "Save layout");
    assert.equal(saveButton.disabled, true);
    const slot = root.querySelectorAll("div").find((el) =>
      el.dataset.slotId === "slot_1");
    assert.ok(slot);
    await slot.fire("pointerdown", { clientX: 0, clientY: 0 });
    await globalThis.document.fire("pointermove", { clientX: 20, clientY: 0 });
    await globalThis.document.fire("pointerup", {});
    assert.equal(saveButton.disabled, false);
    assert.match(root.textContent, /Unsaved layout changes/);
    await saveButton.fire("click");
    assert.match(root.textContent, /Saved to Work database/);
    assert.equal(saveButton.disabled, true);
    assert.equal(server.slots[0].geometry_json.x, 50);
    assert.deepEqual(server.panels[0].generation_spec_json, { seed: 42 });
    dispose();
    assert.equal(root.children.length, 0);
    const second = env.root();
    const dispose2 = editor.mountPageEditor(second);
    await flush();
    assert.match(second.textContent, /Loaded saved Page/);
    const savedSlot = second.querySelectorAll("div").find((el) =>
      el.dataset.slotId === "slot_1");
    assert.equal(savedSlot.style.left, (100 * 50 / 1200) + "%");
    assert.equal(requests.at(-1).url,
      "/manga_autopilot/api/v2/works/work_123/pages/page_1");
    dispose2();
  } finally {
    env.restore();
  }
});

test("409 conflicts and failed saves are visible and cannot show Saved", async () => {
  const env = setupBrowser();
  let failMode = "conflict";
  globalThis.fetch = async (url, options = {}) => {
    if (url.endsWith("/pages")) return response(200, { pages: [persisted().page] });
    if (options.method === "PATCH") {
      if (failMode === "conflict") {
        return response(409, {
          error: "revision_conflict", message: "Layout was updated elsewhere",
          entity_id: "layout_1", expected_revision: 3, actual_revision: 4,
        });
      }
      return response(503, { message: "Save unavailable" });
    }
    return response(200, persisted());
  };
  try {
    const root = env.root();
    const dispose = editor.mountPageEditor(root, { workId: "work_123" });
    await flush();
    const widthInput = find(root, "label", "Page width").querySelectorAll("input")[0];
    widthInput.value = "1500";
    await widthInput.fire("change");
    const saveButton = find(root, "button", "Save layout");
    await saveButton.fire("click");
    assert.match(root.textContent, /Revision conflict/);
    assert.equal(saveButton.disabled, true);
    assert.ok(!root.textContent.includes("Saved to Work database"));
    const reload = find(root, "button", "Reload latest");
    await reload.fire("click");
    assert.match(root.textContent, /Loaded saved Page/);
    assert.equal(widthInput.value, "1200");
    failMode = "failure";
    widthInput.value = "1600";
    await widthInput.fire("change");
    await saveButton.fire("click");
    assert.match(root.textContent, /Save failed: Save unavailable/);
    assert.equal(saveButton.disabled, false);
    assert.ok(!root.textContent.includes("Saved to Work database"));
    dispose();
  } finally {
    env.restore();
  }
});

test("empty Work lists are displayed without fabricated panels or Save", async () => {
  const env = setupBrowser();
  globalThis.fetch = async () => response(200, { pages: [] });
  try {
    const root = env.root();
    editor.mountPageEditor(root, { workId: "work_empty" });
    await flush();
    assert.match(root.textContent, /no persisted Pages/);
    assert.ok(!root.textContent.includes("panel_01"));
    assert.equal(find(root, "button", "Save layout").disabled, true);
  } finally {
    env.restore();
  }
});
