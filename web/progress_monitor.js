// Progress monitor UI (spec section 22.4).
// Polls /manga_autopilot/api/projects/{id}/autopilot/status and renders the
// current state, queue, failed panels, retries, and average QA score.

(function () {
  "use strict";

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
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

  function ProgressMonitor(root, opts = {}) {
    const projectId = opts.projectId;
    if (!projectId) {
      root.appendChild(el("div", { class: "manga-err" }, "projectId is required"));
      return;
    }
    const statusEl = el("pre", { class: "manga-monitor-status" }, "Loading...");
    const logEl = el("ul", { class: "manga-monitor-log" });
    const actions = el(
      "div",
      { class: "manga-monitor-actions" },
      el("button", { type: "button", onClick: () => api(actionPath("/start"), { method: "POST" }) }, "Start"),
      el("button", { type: "button", onClick: () => api(actionPath("/pause"), { method: "POST" }) }, "Pause"),
      el("button", { type: "button", onClick: () => api(actionPath("/resume"), { method: "POST" }) }, "Resume"),
      el("button", { type: "button", onClick: () => api(actionPath("/cancel"), { method: "POST" }) }, "Cancel"),
      el("button", { type: "button", onClick: () => refresh() }, "Refresh")
    );
    const startedEl = el("div", { class: "manga-monitor-started" });
    const finishedEl = el("div", { class: "manga-monitor-finished" });
    const failureEl = el("div", { class: "manga-monitor-failure" });

    function actionPath(suffix) {
      return `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/autopilot${suffix}`;
    }

    async function refresh() {
      try {
        const status = await api(actionPath("/status"));
        statusEl.textContent = JSON.stringify(status, null, 2);
        startedEl.textContent = `Started: ${status.started_at || "-"}`;
        finishedEl.textContent = `Finished: ${status.finished_at || "-"}`;
        failureEl.textContent = status.failure_reason ? `Failure: ${status.failure_reason}` : "";
        logEl.replaceChildren(
          ...(status.log || []).map((entry) =>
            el(
              "li",
              {},
              `${entry.at || ""} ${entry.kind || ""} ${JSON.stringify(entry)}`
            )
          )
        );
      } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
      }
    }

    root.appendChild(actions);
    root.appendChild(el("h3", {}, "Autopilot status"));
    root.appendChild(statusEl);
    root.appendChild(startedEl);
    root.appendChild(finishedEl);
    root.appendChild(failureEl);
    root.appendChild(el("h4", {}, "Log"));
    root.appendChild(logEl);

    refresh();
    if (opts.pollMs) {
      setInterval(refresh, opts.pollMs);
    }
  }

  window.MangaAutopilot = window.MangaAutopilot || {};
  window.MangaAutopilot.mountProgressMonitor = ProgressMonitor;
})();
