/**
 * Work-backed Export Center.
 *
 * Page Editor and PNG export share v2 Work/Page identity, not browser
 * snapshots or legacy projects/<id>/export payloads. The server owns layout
 * and candidate-image selection, and the Work DB owns exported file records.
 */

const API_ROOT = "/manga_autopilot/api/v2/works";
const WORK_PREF = "manga_autopilot_editor_work_id";
const PAGE_PREF = "manga_autopilot_editor_page_id";

export function pagesUrl(workId) {
  if (typeof workId !== "string" || !workId.trim()) {
    throw new Error("A Work ID is required.");
  }
  return API_ROOT + "/" + encodeURIComponent(workId.trim()) + "/pages";
}

export function exportsUrl(workId) {
  return API_ROOT + "/" + encodeURIComponent(required(workId, "Work ID")) + "/exports";
}

export function pngExportUrl(workId, pageId) {
  return pagesUrl(workId) + "/" + encodeURIComponent(required(pageId, "Page ID"))
    + "/export/png";
}

export function exportPngFileUrl(workId, artifactId) {
  return exportsUrl(workId) + "/" + encodeURIComponent(required(artifactId, "Artifact ID"))
    + "/png";
}

function required(value, field) {
  if (typeof value !== "string" || !value.trim()) {
    throw new Error(field + " is required.");
  }
  return value.trim();
}

export class ExportCenterError extends Error {
  constructor(message, status, details = {}) {
    super(message);
    this.name = "ExportCenterError";
    this.status = status;
    this.details = details;
  }
}

async function requestJson(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
  } catch (err) {
    throw new ExportCenterError("Network error: " + err.message, 0);
  }
  const content = await response.text();
  let payload;
  try {
    payload = content ? JSON.parse(content) : null;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new ExportCenterError(
      (payload && (payload.message || payload.error)) || content
      || response.statusText || "Request failed",
      response.status,
      payload || {},
    );
  }
  if (!payload || typeof payload !== "object") {
    throw new ExportCenterError("Invalid JSON response from server.", response.status);
  }
  return payload;
}

export async function listWorkPages(workId) {
  const data = await requestJson(pagesUrl(workId));
  if (!Array.isArray(data.pages)) {
    throw new ExportCenterError("Missing persisted Page list.", 200);
  }
  return data.pages;
}

export async function listWorkExports(workId) {
  const id = required(workId, "Work ID");
  const data = await requestJson(exportsUrl(id));
  if (data.work_id !== id || !Array.isArray(data.exports)) {
    throw new ExportCenterError("Invalid Work Export list response.", 200);
  }
  return data.exports;
}

export async function exportSavedPagePng(workId, pageId, settings = {}) {
  const id = required(workId, "Work ID");
  const page = required(pageId, "Page ID");
  if (settings === null || typeof settings !== "object" || Array.isArray(settings)) {
    throw new TypeError("Export settings must be an object.");
  }
  const invalid = Object.keys(settings).filter((key) =>
    key !== "background" && key !== "outer_border" && key !== "export_profile");
  if (invalid.length) {
    throw new Error("Unsupported export settings: " + invalid.join(", "));
  }
  // No opts.pages, opts.pagePngs, layout coordinates, or local file paths.
  const data = await requestJson(pngExportUrl(id, page), {
    method: "POST",
    body: JSON.stringify(settings),
  });
  if (data.work_id !== id || data.page_id !== page || !data.artifact_id
      || !data.relative_path) {
    throw new ExportCenterError(
      "PNG export returned an invalid Work Artifact response.", 201
    );
  }
  return data;
}

function element(tag, text = "", className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function readPreference(name) {
  try {
    return globalThis.localStorage?.getItem(name) || "";
  } catch {
    return "";
  }
}

function writePreference(name, value) {
  try {
    globalThis.localStorage?.setItem(name, value);
  } catch {
    // Preferences are optional. Work SQLite is always the authority.
  }
}

export function mountExportCenter(root, opts = {}) {
  let disposed = false;
  let requestToken = 0;
  let busy = false;
  let loadedWorkId = "";
  let loadedPageId = "";

  root.replaceChildren();
  const title = element("h3", "Work PNG exports");
  const toolbar = element("div", "", "manga-export-toolbar");
  toolbar.style.display = "flex";
  toolbar.style.flexWrap = "wrap";
  toolbar.style.gap = "8px";

  const workLabel = element("label", "Work ID");
  const workInput = document.createElement("input");
  workInput.type = "text";
  workInput.placeholder = "Existing Work ID";
  workInput.value = opts.workId || readPreference(WORK_PREF);
  workLabel.appendChild(workInput);
  toolbar.appendChild(workLabel);
  const pagesButton = element("button", "Load Pages");
  pagesButton.type = "button";
  toolbar.appendChild(pagesButton);

  const pageLabel = element("label", "Saved Page");
  const pageSelect = document.createElement("select");
  pageLabel.appendChild(pageSelect);
  toolbar.appendChild(pageLabel);
  const profileLabel = element("label", "PNG profile");
  const profileSelect = document.createElement("select");
  for (const [value, caption] of [
    ["screen", "Screen (up to 12 MP)"],
    ["print", "Print (up to 24 MP)"],
  ]) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = caption;
    profileSelect.appendChild(option);
  }
  profileSelect.value = "screen";
  profileLabel.appendChild(profileSelect);
  toolbar.appendChild(profileLabel);
  const exportButton = element("button", "Export PNG");
  exportButton.type = "button";
  toolbar.appendChild(exportButton);
  const refreshButton = element("button", "Refresh exports");
  refreshButton.type = "button";
  toolbar.appendChild(refreshButton);

  const status = element("p", "Enter an existing Work ID to load saved Pages.");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const result = element("div", "", "manga-export-result");
  const heading = element("h4", "Registered Work exports");
  const list = element("ul", "", "manga-export-list");
  root.append(title, toolbar, status, result, heading, list);

  const legacy = opts.projectId ? element("details") : null;
  const legacyList = legacy ? element("ul", "", "manga-legacy-export-list") : null;
  if (legacy) {
    legacy.appendChild(element("summary", "Legacy Project export list (read-only)"));
    legacy.appendChild(element("p",
      "Old Project exports are separate from the Work Artifact registry."));
    legacy.appendChild(legacyList);
    const legacyButton = element("button", "Refresh legacy list");
    legacyButton.type = "button";
    legacy.appendChild(legacyButton);
    legacyButton.addEventListener("click", () => void refreshLegacy());
    root.appendChild(legacy);
  }

  function updateControls() {
    pagesButton.disabled = busy;
    workInput.disabled = busy;
    pageSelect.disabled = busy || !loadedWorkId || !pageSelect.options.length;
    profileSelect.disabled = busy;
    exportButton.disabled = busy || !loadedWorkId || !loadedPageId;
    refreshButton.disabled = busy || !loadedWorkId;
  }

  function showStatus(message, isError = false) {
    status.textContent = message;
    status.style.color = isError ? "var(--error-text, #d44)" : "";
  }

  function showError(action, workId, pageId, err) {
    const scope = pageId
      ? "Work " + workId + ", Page " + pageId
      : "Work " + workId;
    const code = err instanceof ExportCenterError && err.status
      ? " (HTTP " + err.status + ")" : "";
    showStatus(action + " failed for " + scope + code + ": " + err.message, true);
  }

  function renderExports(entries) {
    list.replaceChildren();
    if (!entries.length) {
      list.appendChild(element("li", "No registered PNG exports for this Work."));
      return;
    }
    for (const entry of entries) {
      // READY is immutable publication status; freshness is separate.
      const freshness = entry.freshness === "CURRENT" ? "CURRENT"
        : entry.freshness === "STALE" ? "STALE" : "UNVERIFIED";
      const label = {
        CURRENT: "Current", STALE: "Stale (historical)",
        UNVERIFIED: "Unverified (historical)",
      }[freshness];
      const item = element("li", "", "manga-export-" + freshness.toLowerCase());
      item.appendChild(element("strong", label + " · "));
      const caption = [
        "Page " + entry.scope_id,
        entry.relative_path,
        entry.width && entry.height ? entry.width + " × " + entry.height : "",
      ].filter(Boolean).join(" · ");
      const link = element("a", caption);
      link.href = exportPngFileUrl(loadedWorkId, entry.id);
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.appendChild(link);
      if (freshness !== "CURRENT") {
        const hint = entry.freshness_reason === "page_inputs_changed"
          ? " · Saved Page changed; export again for current output."
          : entry.freshness_reason === "candidate_choice_changed"
            ? " · Candidate choice changed; export again."
            : " · Current provenance cannot be confirmed.";
        item.appendChild(element("span", hint));
      }
      list.appendChild(item);
    }
  }

  async function refreshExports() {
    if (!loadedWorkId || disposed) return;
    const token = ++requestToken;
    busy = true;
    updateControls();
    showStatus("Loading registered Work exports...");
    try {
      const entries = await listWorkExports(loadedWorkId);
      if (disposed || token !== requestToken) return;
      renderExports(entries);
      showStatus("Loaded " + entries.length + " registered Work PNG exports.");
    } catch (err) {
      if (disposed || token !== requestToken) return;
      showError("Export listing", loadedWorkId, null, err);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  }

  async function loadPages() {
    const workId = workInput.value.trim();
    const token = ++requestToken;
    loadedWorkId = "";
    loadedPageId = "";
    pageSelect.replaceChildren();
    list.replaceChildren();
    result.replaceChildren();
    busy = true;
    updateControls();
    showStatus("Loading saved Work Pages...");
    try {
      const pages = await listWorkPages(workId);
      if (disposed || token !== requestToken) return;
      loadedWorkId = workId;
      writePreference(WORK_PREF, workId);
      const preferred = opts.pageId || readPreference(PAGE_PREF);
      for (const page of pages) {
        const option = document.createElement("option");
        option.value = page.id;
        option.textContent = "Page " + page.page_number + " (" + page.id + ")";
        pageSelect.appendChild(option);
      }
      loadedPageId = pages.some((page) => page.id === preferred)
        ? preferred : (pages[0]?.id || "");
      pageSelect.value = loadedPageId;
      if (loadedPageId) writePreference(PAGE_PREF, loadedPageId);
      const exports = await listWorkExports(workId);
      if (disposed || token !== requestToken) return;
      renderExports(exports);
      showStatus(pages.length
        ? "Loaded " + pages.length + " saved Pages and " + exports.length + " exports."
        : "This Work has no saved Pages. " + exports.length + " existing exports found.");
    } catch (err) {
      if (disposed || token !== requestToken) return;
      showError("Load", workId, null, err);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  }

  async function exportPage() {
    if (busy || !loadedWorkId || !loadedPageId || disposed) return;
    const token = ++requestToken;
    const work = loadedWorkId;
    const page = loadedPageId;
    busy = true;
    updateControls();
    showStatus("Exporting saved Page " + page + " in Work " + work + "...");
    result.replaceChildren();
    try {
      const settings = profileSelect.value === "print"
        ? { export_profile: "print" } : {};
      const exported = await exportSavedPagePng(work, page, settings);
      if (disposed || token !== requestToken) return;
      result.appendChild(element("p",
        "PNG registered: " + exported.relative_path));
      const entries = await listWorkExports(work);
      if (disposed || token !== requestToken) return;
      renderExports(entries);
      showStatus("PNG exported and registered for Work " + work + ", Page " + page + ".");
    } catch (err) {
      if (disposed || token !== requestToken) return;
      showError("PNG export", work, page, err);
    } finally {
      if (!disposed && token === requestToken) {
        busy = false;
        updateControls();
      }
    }
  }

  async function refreshLegacy() {
    if (!legacyList || disposed) return;
    legacyList.replaceChildren(element("li", "Loading legacy Project exports..."));
    try {
      const project = required(opts.projectId, "Project ID");
      const data = await requestJson(
        "/manga_autopilot/api/projects/" + encodeURIComponent(project) + "/exports"
      );
      if (disposed) return;
      if (!Array.isArray(data.files)) {
        throw new ExportCenterError("Invalid legacy Project export list.", 200);
      }
      legacyList.replaceChildren();
      for (const name of data.files) {
        legacyList.appendChild(element("li", String(name)));
      }
      if (!data.files.length) legacyList.appendChild(element("li", "No legacy exports."));
    } catch (err) {
      if (!disposed) {
        legacyList.replaceChildren(element("li", "Legacy listing: " + err.message));
      }
    }
  }

  pagesButton.addEventListener("click", loadPages);
  pageSelect.addEventListener("change", () => {
    loadedPageId = pageSelect.value;
    writePreference(PAGE_PREF, loadedPageId);
    updateControls();
  });
  exportButton.addEventListener("click", exportPage);
  refreshButton.addEventListener("click", refreshExports);
  updateControls();

  if (workInput.value.trim()) void loadPages();
  return () => {
    disposed = true;
    ++requestToken;
    root.replaceChildren();
  };
}

// Keep legacy ComfyUI global mounts working without maintaining a second
// implementation. The ES module above is the authoritative UI.
if (typeof window !== "undefined") {
  window.MangaAutopilot = window.MangaAutopilot || {};
  window.MangaAutopilot.mountExportCenter = mountExportCenter;
}
