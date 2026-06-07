// Export Center UI (spec section 22).
// Lists existing exports for a project and lets the user trigger PNG, webtoon,
// and PDF exports via the HTTP API.

(function () {
  "use strict";

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v !== undefined && v !== null) {
        node.setAttribute(k, v);
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function api(path, options = {}) {
    return fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    }).then(async (r) => {
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    });
  }

  function ExportCenter(root, opts = {}) {
    const projectId = opts.projectId;
    if (!projectId) {
      root.appendChild(el("div", { class: "manga-err" }, "projectId is required"));
      return;
    }
    const listEl = el("ul", { class: "manga-export-list" });
    const resultEl = el("pre", { class: "manga-export-result" });

    function base() {
      return `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/export`;
    }

    async function refresh() {
      try {
        const data = await api(base() + "s");
        listEl.replaceChildren(
          ...(data.files || []).map((f) =>
            el("li", {}, f.replace(/^.*\/projects\/[^/]+\/exports\//, ""))
          )
        );
      } catch (err) {
        listEl.replaceChildren(el("li", { class: "manga-err" }, err.message));
      }
    }

    async function callExport(suffix, body) {
      resultEl.textContent = "Working...";
      try {
        const data = await api(base() + suffix, { method: "POST", body: JSON.stringify(body || {}) });
        resultEl.textContent = JSON.stringify(data, null, 2);
        await refresh();
      } catch (err) {
        resultEl.textContent = `Error: ${err.message}`;
      }
    }

    root.appendChild(
      el(
        "div",
        { class: "manga-export-toolbar" },
        el("button", { type: "button", onClick: () => callExport("/png", { pages: opts.pages || {} }) }, "Export PNG"),
        el("button", { type: "button", onClick: () => callExport("/webtoon", { page_pngs: opts.pagePngs || [] }) }, "Export Webtoon"),
        el("button", { type: "button", onClick: () => callExport("/pdf", { page_pngs: opts.pagePngs || [] }) }, "Export PDF"),
        el("button", { type: "button", onClick: refresh }, "Refresh")
      )
    );
    root.appendChild(el("h3", {}, "Exports"));
    root.appendChild(listEl);
    root.appendChild(resultEl);
    refresh();
  }

  window.MangaAutopilot = window.MangaAutopilot || {};
  window.MangaAutopilot.mountExportCenter = ExportCenter;
})();

export function mountExportCenter(container, opts) {
  return window.MangaAutopilot?.mountExportCenter?.(container, opts);
}
