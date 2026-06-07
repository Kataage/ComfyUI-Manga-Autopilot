/**
 * Manga Autopilot page editor (spec sections 22.5 and 25.1)
 *
 * Drag/resize panels and save the layout back to the backend. The Konva
 * canvas and React component live in the ComfyUI frontend bundle; this
 * module provides the registration entry point so the page is reachable
 * from the sidebar tab declared in `index.js`.
 *
 * The heavy lifting is intentionally kept in a single vanilla-JS shim so
 * the rest of the ComfyUI frontend (which is plain JS) can require it
 * without an extra build step. A future PR may swap this for a React
 * component bundled separately.
 */

const PANEL_DEFAULT = Object.freeze({
  width: 320,
  height: 240,
  x: 40,
  y: 40,
  zIndex: 0,
});

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function createPanelElement(panel, pageWidth, pageHeight, onChange) {
  const root = document.createElement("div");
  root.className = "manga-autopilot-panel";
  root.style.position = "absolute";
  root.style.left = `${(panel.x / pageWidth) * 100}%`;
  root.style.top = `${(panel.y / pageHeight) * 100}%`;
  root.style.width = `${(panel.width / pageWidth) * 100}%`;
  root.style.height = `${(panel.height / pageHeight) * 100}%`;
  root.style.border = "2px solid #222";
  root.style.background = "rgba(255, 255, 255, 0.85)";
  root.style.cursor = "move";
  root.style.boxSizing = "border-box";
  root.style.zIndex = String(panel.zIndex ?? 0);
  root.textContent = panel.panel_id || "panel";
  root.dataset.panelId = panel.panel_id;

  // Drag (mouse) — keeps everything in normalised page coords.
  let dragging = false;
  let offsetX = 0;
  let offsetY = 0;
  root.addEventListener("mousedown", (event) => {
    dragging = true;
    const rect = root.getBoundingClientRect();
    offsetX = event.clientX - rect.left;
    offsetY = event.clientY - rect.top;
    event.preventDefault();
  });
  document.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    const parent = root.parentElement;
    if (!parent) return;
    const parentRect = parent.getBoundingClientRect();
    const newX = clamp(event.clientX - parentRect.left - offsetX, 0, parentRect.width - root.offsetWidth);
    const newY = clamp(event.clientY - parentRect.top - offsetY, 0, parentRect.height - root.offsetHeight);
    root.style.left = `${(newX / parentRect.width) * 100}%`;
    root.style.top = `${(newY / parentRect.height) * 100}%`;
    onChange({ ...panel, x: (newX / parentRect.width) * pageWidth, y: (newY / parentRect.height) * pageHeight });
  });
  document.addEventListener("mouseup", () => {
    dragging = false;
  });

  return root;
}

/**
 * Mount the page editor into ``container``. Returns a dispose function.
 *
 * @param {HTMLElement} container
 * @param {{ projectId: string, pageNumber: number, pageWidth?: number, pageHeight?: number }} opts
 */
export function mountPageEditor(container, opts) {
  const pageWidth = opts.pageWidth ?? 1200;
  const pageHeight = opts.pageHeight ?? 1600;
  const panels = Array.isArray(opts.panels) ? opts.panels.slice() : [];

  container.innerHTML = "";
  const stage = document.createElement("div");
  stage.className = "manga-autopilot-editor-stage";
  stage.style.position = "relative";
  stage.style.width = `${pageWidth}px`;
  stage.style.height = `${pageHeight}px`;
  stage.style.background = "#fafafa";
  stage.style.border = "1px solid #ccc";
  stage.style.overflow = "hidden";

  function rerender(nextPanels) {
    stage.innerHTML = "";
    for (const panel of nextPanels) {
      stage.appendChild(createPanelElement(panel, pageWidth, pageHeight, (updated) => {
        const idx = nextPanels.findIndex((p) => p.panel_id === updated.panel_id);
        if (idx >= 0) nextPanels[idx] = updated;
      }));
    }
  }

  rerender(panels.length ? panels : [{ ...PANEL_DEFAULT, panel_id: "panel_01" }]);

  const toolbar = document.createElement("div");
  toolbar.className = "manga-autopilot-editor-toolbar";
  toolbar.style.marginBottom = "8px";
  toolbar.style.display = "flex";
  toolbar.style.gap = "8px";

  const addBtn = document.createElement("button");
  addBtn.textContent = "Add panel";
  addBtn.onclick = () => {
    const idx = stage.children.length + 1;
    panels.push({
      ...PANEL_DEFAULT,
      panel_id: `panel_${String(idx).padStart(2, "0")}`,
    });
    rerender(panels);
  };
  toolbar.appendChild(addBtn);

  const saveBtn = document.createElement("button");
  saveBtn.textContent = "Save layout";
  saveBtn.onclick = async () => {
    const url = `/manga_autopilot/api/projects/${encodeURIComponent(opts.projectId)}/pages/${opts.pageNumber}/layout`;
    const response = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_width: pageWidth, page_height: pageHeight, panels }),
    });
    if (!response.ok) {
      const text = await response.text();
      saveBtn.textContent = `Save failed: ${text}`;
      return;
    }
    saveBtn.textContent = "Saved!";
    setTimeout(() => (saveBtn.textContent = "Save layout"), 1500);
  };
  toolbar.appendChild(saveBtn);

  container.appendChild(toolbar);
  container.appendChild(stage);

  return () => {
    container.innerHTML = "";
  };
}

export const __test__ = { clamp, PANEL_DEFAULT };
