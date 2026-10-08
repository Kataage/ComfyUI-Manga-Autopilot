// Native Node integration-style browser shim tests for Work Export Center.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";

const source = await readFile(join(process.cwd(), "web", "export_center.js"), "utf8");
const editor = await import("data:text/javascript;base64,"
  + Buffer.from(source).toString("base64"));

function response(status, data) {
  return {
    ok: status >= 200 && status < 300,
    status, statusText: "HTTP " + status,
    async text() { return JSON.stringify(data); },
  };
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.style = {};
    this.events = new Map();
    this._text = "";
    this.value = "";
    this.disabled = false;
  }
  set textContent(value) {
    this._text = String(value);
    this.replaceChildren();
  }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join("");
  }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  append(...children) { for (const child of children) this.appendChild(child); }
  replaceChildren(...children) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    for (const child of children) this.appendChild(child);
  }
  get options() { return this.children.filter((c) => c.tagName === "OPTION"); }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(event, fn) {
    const fns = this.events.get(event) || [];
    fns.push(fn);
    this.events.set(event, fns);
  }
  async fire(event) {
    await Promise.all((this.events.get(event) || []).map((fn) => fn()));
  }
  querySelectorAll(tag) {
    const results = [];
    const key = tag.toUpperCase();
    function visit(node) {
      for (const child of node.children) {
        if (child.tagName === key) results.push(child);
        visit(child);
      }
    }
    visit(this);
    return results;
  }
}

function find(root, tag, label) {
  const result = root.querySelectorAll(tag).find((el) => el.textContent.includes(label));
  assert.ok(result, tag + " containing " + label + " should be present");
  return result;
}

function browser() {
  const old = {
    document: globalThis.document, fetch: globalThis.fetch,
    localStorage: globalThis.localStorage,
  };
  globalThis.document = { createElement: (tag) => new FakeElement(tag) };
  const prefs = new Map();
  globalThis.localStorage = {
    getItem: (k) => prefs.get(k) || null,
    setItem: (k, v) => prefs.set(k, String(v)),
  };
  return {
    root() { return new FakeElement("div"); },
    preferences: prefs,
    restore() {
      globalThis.document = old.document;
      globalThis.fetch = old.fetch;
      globalThis.localStorage = old.localStorage;
    },
  };
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 10));
}

test("v2 paths and request payload exclude browser layout and file paths", async () => {
  assert.equal(editor.pagesUrl("work/x"),
    "/manga_autopilot/api/v2/works/work%2Fx/pages");
  assert.equal(editor.pngExportUrl("work/x", "page a"),
    "/manga_autopilot/api/v2/works/work%2Fx/pages/page%20a/export/png");
  assert.equal(editor.exportsUrl("work/x"),
    "/manga_autopilot/api/v2/works/work%2Fx/exports");
  assert.equal(editor.exportPngFileUrl("work/x", "artifact_1"),
    "/manga_autopilot/api/v2/works/work%2Fx/exports/artifact_1/png");
  assert.throws(() => editor.pagesUrl(" "), /Work ID/);
  assert.throws(() => editor.pngExportUrl("work", " "), /Page ID/);
  await assert.rejects(editor.exportSavedPagePng("work", "page", {
    pages: { page: [] },
  }), /Unsupported export settings/);
  await assert.rejects(editor.exportSavedPagePng("work", "page", {
    pagePngs: ["x.png"],
  }), /Unsupported export settings/);
});

test("Work context loads persisted Pages, exports PNG, refreshes registered list", async () => {
  const env = browser();
  const calls = [];
  let completed = false;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, options });
    if (url.endsWith("/pages")) {
      return response(200, { pages: [{ id: "page_one", page_number: 1 }] });
    }
    if (url.endsWith("/exports")) {
      return response(200, {
        work_id: "work_valid",
        exports: completed ? [{
          id: "png_a", scope_id: "page_one", width: 280, height: 210,
          relative_path: "exports/pages/page_one_png_a.png",
        }] : [],
      });
    }
    if (url.endsWith("/export/png")) {
      assert.equal(options.method, "POST");
      assert.deepEqual(JSON.parse(options.body), {});
      completed = true;
      return response(201, {
        work_id: "work_valid", page_id: "page_one",
        artifact_id: "png_a",
        relative_path: "exports/pages/page_one_png_a.png",
      });
    }
    throw new Error("Unexpected request " + url);
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, {
      workId: "work_valid", pageId: "page_one",
      pages: { nonexistent: [{ panel_id: "fake" }] },
      pagePngs: ["/tmp/client-state.png"],
    });
    await flush();
    assert.match(root.textContent, /Loaded 1 saved Pages/);
    assert.equal(find(root, "button", "Export PNG").disabled, false);
    assert.match(root.textContent, /No registered PNG exports/);
    await find(root, "button", "Export PNG").fire("click");
    assert.match(root.textContent, /PNG exported and registered/);
    assert.ok(root.textContent.includes("page_one_png_a.png"));
    const links = root.querySelectorAll("a");
    assert.equal(links.length, 1);
    assert.equal(links[0].href,
      "/manga_autopilot/api/v2/works/work_valid/exports/png_a/png");
    assert.equal(links[0].rel, "noopener noreferrer");
    assert.deepEqual(calls.map((req) => req.url), [
      "/manga_autopilot/api/v2/works/work_valid/pages",
      "/manga_autopilot/api/v2/works/work_valid/exports",
      "/manga_autopilot/api/v2/works/work_valid/pages/page_one/export/png",
      "/manga_autopilot/api/v2/works/work_valid/exports",
    ]);
    assert.equal(env.preferences.get("manga_autopilot_editor_work_id"), "work_valid");
    dispose();
    assert.equal(root.children.length, 0);
  } finally {
    env.restore();
  }
});

test("remount from Page Editor shared preferences loads selected Page", async () => {
  const env = browser();
  env.preferences.set("manga_autopilot_editor_work_id", "work_remembered");
  env.preferences.set("manga_autopilot_editor_page_id", "page_second");
  let requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/pages")) {
      return response(200, { pages: [
        { id: "page_first", page_number: 1 },
        { id: "page_second", page_number: 2 },
      ] });
    }
    if (url.endsWith("/exports")) {
      return response(200, { work_id: "work_remembered", exports: [] });
    }
    if (url.endsWith("/export/png")) {
      return response(201, {
        work_id: "work_remembered", page_id: "page_second",
        artifact_id: "png_result", relative_path: "exports/pages/png_result.png",
      });
    }
    throw new Error("Unexpected URL " + url);
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root);
    await flush();
    const selector = find(root, "label", "Saved Page").querySelectorAll("select")[0];
    assert.equal(selector.value, "page_second");
    await find(root, "button", "Export PNG").fire("click");
    assert.equal(requests[2].url,
      "/manga_autopilot/api/v2/works/work_remembered/pages/page_second/export/png");
    dispose();
  } finally {
    env.restore();
  }
});

test("HTTP 422 export precondition shows the specific Work, Page and prerequisite", async () => {
  const env = browser();
  const urls = [];
  globalThis.fetch = async (url, options = {}) => {
    urls.push(url);
    if (url.endsWith("/pages")) {
      return response(200, { pages: [{ id: "page_missing", page_number: 2 }] });
    }
    if (url.endsWith("/exports")) {
      return response(200, { work_id: "work_missing_image", exports: [] });
    }
    if (options.method === "POST") {
      return response(422, {
        error: "export_precondition_failed",
        message: "Panel panel_unbound: no LayoutSlot binding. Bind this Panel.",
      });
    }
    throw new Error("Unexpected URL " + url);
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, { workId: "work_missing_image" });
    await flush();
    await find(root, "button", "Export PNG").fire("click");
    assert.match(root.textContent, /PNG export failed for Work work_missing_image, Page page_missing/);
    assert.match(root.textContent, /HTTP 422/);
    assert.match(root.textContent, /Bind this Panel/);
    assert.ok(!root.textContent.includes("PNG exported and registered"));
    assert.equal(find(root, "button", "Export PNG").disabled, false);
    assert.equal(urls.filter((x) => x.endsWith("/exports")).length, 1);
    dispose();
  } finally {
    env.restore();
  }
});

test("empty persisted Page list disables PNG export and still shows Work exports", async () => {
  const env = browser();
  let posts = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "POST") posts++;
    if (url.endsWith("/pages")) return response(200, { pages: [] });
    return response(200, { work_id: "work_empty", exports: [] });
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, { workId: "work_empty" });
    await flush();
    assert.match(root.textContent, /no saved Pages/);
    assert.equal(find(root, "button", "Export PNG").disabled, true);
    await find(root, "button", "Export PNG").fire("click");
    assert.equal(posts, 0);
    dispose();
  } finally {
    env.restore();
  }
});

test("legacy Project export list remains accessible only in separate opt-in section", async () => {
  const env = browser();
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    if (url.endsWith("/pages")) return response(200, { pages: [] });
    if (url.endsWith("/exports") && url.includes("/api/v2/")) {
      return response(200, { work_id: "work_valid", exports: [] });
    }
    if (url.endsWith("/api/projects/legacy_one/exports")) {
      return response(200, { files: ["/legacy/exports/page_0001.png"] });
    }
    throw new Error("Unexpected URL " + url);
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, {
      workId: "work_valid", projectId: "legacy_one",
    });
    await flush();
    assert.equal(urls.some((u) => u.includes("/api/projects/")), false);
    const detail = root.querySelectorAll("details");
    assert.equal(detail.length, 1);
    assert.match(detail[0].textContent, /read-only/);
    await find(root, "button", "Refresh legacy list").fire("click");
    assert.equal(urls.some((u) => u.endsWith("/api/projects/legacy_one/exports")), true);
    assert.match(root.textContent, /page_0001.png/);
    dispose();
  } finally {
    env.restore();
  }
});

test("disposed Export Center ignores delayed server results", async () => {
  const env = browser();
  let release;
  globalThis.fetch = async () => new Promise((resolve) => { release = resolve; });
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, { workId: "work_slow" });
    dispose();
    assert.equal(root.children.length, 0);
    release(response(200, { pages: [{ id: "page_1", page_number: 1 }] }));
    await flush();
    assert.equal(root.children.length, 0);
  } finally {
    env.restore();
  }
});


test("explicit Print profile is sent as bounded settings, never geometry", async () => {
  const env = browser();
  const exports = [];
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    if (url.endsWith("/pages")) {
      return response(200, { pages: [{ id: "page_print", page_number: 3 }] });
    }
    if (url.endsWith("/exports")) {
      return response(200, { work_id: "work_print", exports });
    }
    if (url.endsWith("/export/png")) {
      const body = JSON.parse(options.body);
      assert.deepEqual(body, { export_profile: "print" });
      return response(201, {
        work_id: "work_print", page_id: "page_print",
        artifact_id: "artifact_print",
        relative_path: "exports/pages/page_print_artifact_print.png",
      });
    }
    throw new Error("unexpected URL " + url);
  };
  try {
    const root = env.root();
    const dispose = editor.mountExportCenter(root, { workId: "work_print" });
    await flush();
    const selects = root.querySelectorAll("select");
    assert.equal(selects.length, 2, "saved Page and explicit PNG profile selectors");
    selects[1].value = "print";
    await find(root, "button", "Export PNG").fire("click");
    assert.match(root.textContent, /PNG exported and registered/);
    assert.equal(requests.filter((r) => r.options.method === "POST").length, 1);
    dispose();
  } finally {
    env.restore();
  }
});
