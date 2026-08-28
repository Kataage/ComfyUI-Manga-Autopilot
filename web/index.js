/**
 * ComfyUI Manga Autopilot web extension entry point.
 *
 * Registers a sidebar tab against the ComfyUI client. The sidebar hosts a
 * workspace with several sub-views (Projects, Page Editor, Character
 * Manager, Progress Monitor, Export Center) that are mounted on demand
 * from the companion modules under `web/`.
 *
 * Spec reference: comfyui_manga_autopilot_spec.md section 11, 22, 25.
 */

import { app } from "../../scripts/app.js";

import { mountPageEditor } from "./page_editor.js";
import { mountCharacterManager } from "./character_manager.js";
import { mountProgressMonitor } from "./progress_monitor.js";
import { mountExportCenter } from "./export_center.js";
import { mountReviewEditor } from "./review_editor.js";

export const EXTENSION_NAME = "comfyui.manga.autopilot";
export const SIDEBAR_TAB_ID = "manga-autopilot";
export const SIDEBAR_TAB_TITLE = "Manga Autopilot";

function resolveMounts() {
    // Prefer ES-module exports; fall back to the window.MangaAutopilot
    // shims (the original IIFE-based mounts) for older ComfyUI loaders.
    return {
        mountPageEditor: mountPageEditor
            || window.MangaAutopilot?.mountPageEditor
            || null,
        mountCharacterManager: mountCharacterManager
            || window.MangaAutopilot?.mountCharacterManager
            || null,
        mountProgressMonitor: mountProgressMonitor
            || window.MangaAutopilot?.mountProgressMonitor
            || null,
        mountExportCenter: mountExportCenter
            || window.MangaAutopilot?.mountExportCenter
            || null,
        mountReviewEditor: mountReviewEditor
            || window.MangaAutopilot?.mountReviewEditor
            || null,
    };
}

const TABS = [
    { id: "projects", label: "Projects" },
    { id: "editor", label: "Page Editor" },
    { id: "characters", label: "Character Manager" },
    { id: "progress", label: "Progress" },
    { id: "reviews", label: "Reviews" },
    { id: "export", label: "Export Center" },
];

let activeProjectId = null;

function createTabBar(onSelect) {
    const bar = document.createElement("div");
    bar.className = "manga-autopilot-tabs";
    bar.style.display = "flex";
    // ComfyUI's sidebar panel is ~312px wide by default and clips overflow on
    // the x axis; six tabs in a single row measure ~448px, which pushed the
    // left-hand tabs out of reach. Wrap instead of overflowing.
    bar.style.flexWrap = "wrap";
    bar.style.gap = "4px";
    bar.style.borderBottom = "1px solid var(--border-color, #444)";
    bar.style.marginBottom = "12px";
    for (const tab of TABS) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = tab.label;
        btn.dataset.tabId = tab.id;
        btn.style.padding = "6px 10px";
        btn.style.background = "transparent";
        btn.style.color = "var(--input-text, #ddd)";
        btn.style.border = "1px solid transparent";
        btn.style.borderRadius = "4px";
        btn.style.cursor = "pointer";
        btn.addEventListener("click", () => onSelect(tab.id));
        bar.appendChild(btn);
    }
    return bar;
}

function highlightTab(bar, activeId) {
    for (const btn of bar.querySelectorAll("button[data-tab-id]")) {
        if (btn.dataset.tabId === activeId) {
            btn.style.background = "var(--comfy-input-bg, #2a2a2a)";
            btn.style.border = "1px solid var(--border-color, #666)";
        } else {
            btn.style.background = "transparent";
            btn.style.border = "1px solid transparent";
        }
    }
}

function createProjectsView() {
    const root = document.createElement("div");
    root.className = "manga-autopilot-projects";

    const heading = document.createElement("h3");
    heading.textContent = "Projects";
    heading.style.marginTop = "0";
    root.appendChild(heading);

    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.flexWrap = "wrap";
    row.style.gap = "8px";
    root.appendChild(row);

    const list = document.createElement("ul");
    list.style.listStyle = "none";
    list.style.padding = "0";
    list.style.margin = "0";
    list.style.maxHeight = "320px";
    list.style.overflowY = "auto";
    root.appendChild(list);

    const refresh = async () => {
        list.replaceChildren();
        try {
            const resp = await fetch("/manga_autopilot/api/health");
            if (!resp.ok) throw new Error("api unreachable");
            const info = document.createElement("li");
            info.textContent = "Backend API is reachable.";
            info.style.padding = "4px 0";
            list.appendChild(info);
            const project = document.createElement("li");
            project.style.padding = "4px 0";
            project.textContent = `Active project: ${activeProjectId ?? "(none)"}`;
            list.appendChild(project);
        } catch (err) {
            const li = document.createElement("li");
            li.textContent = `Backend unreachable: ${err.message}`;
            li.style.color = "var(--error-text, #f88)";
            list.appendChild(li);
        }
    };

    const setActive = document.createElement("input");
    setActive.placeholder = "project id (e.g. demo)";
    setActive.style.flex = "1";
    setActive.style.padding = "4px 6px";
    setActive.style.background = "var(--comfy-input-bg, #1a1a1a)";
    setActive.style.color = "var(--input-text, #ddd)";
    setActive.style.border = "1px solid var(--border-color, #444)";
    setActive.style.borderRadius = "3px";
    row.appendChild(setActive);

    const activate = document.createElement("button");
    activate.textContent = "Set active";
    activate.style.padding = "4px 10px";
    activate.onclick = () => {
        const v = setActive.value.trim();
        if (!v) return;
        activeProjectId = v;
        refresh();
    };
    row.appendChild(activate);

    const refreshBtn = document.createElement("button");
    refreshBtn.textContent = "Refresh";
    refreshBtn.style.padding = "4px 10px";
    refreshBtn.onclick = refresh;
    row.appendChild(refreshBtn);

    refresh();
    return root;
}

function createWorkspaceView() {
    const root = document.createElement("div");
    root.className = "manga-autopilot-workspace";

    const tabs = createTabBar((id) => {
        highlightTab(tabs, id);
        showTab(id);
    });
    root.appendChild(tabs);

    const content = document.createElement("div");
    content.className = "manga-autopilot-content";
    root.appendChild(content);

    const disposers = new Map();
    const mounts = resolveMounts();

    const showTab = (id) => {
        const prev = disposers.get(activeTab);
        if (typeof prev === "function") prev();
        content.replaceChildren();

        // Projects is the only view that can set the active project id, so it
        // has to render before the guard below - otherwise a fresh workspace
        // has no way out of "set an active project id".
        if (id === "projects") {
            content.appendChild(createProjectsView());
            activeTab = id;
            return;
        }

        if (!activeProjectId) {
            const msg = document.createElement("p");
            msg.textContent = "Set an active project id in the Projects tab to continue.";
            content.appendChild(msg);
            activeTab = id;
            return;
        }

        const mountInto = (mountFn, key) => {
            const host = document.createElement("div");
            content.appendChild(host);
            if (typeof mountFn === "function") {
                try {
                    disposers.set(key, mountFn(host, { projectId: activeProjectId }));
                } catch (err) {
                    const errEl = document.createElement("pre");
                    errEl.textContent = `${key} mount failed: ${err.message}`;
                    errEl.style.color = "var(--error-text, #f88)";
                    host.replaceChildren(errEl);
                }
            } else {
                const placeholder = document.createElement("p");
                placeholder.textContent = `${key} is not available in this build.`;
                placeholder.style.opacity = "0.7";
                host.appendChild(placeholder);
            }
        };

        if (id === "editor") {
            mountInto(mounts.mountPageEditor, "editor");
        } else if (id === "characters") {
            mountInto(mounts.mountCharacterManager, "characters");
        } else if (id === "progress") {
            mountInto(mounts.mountProgressMonitor, "progress");
        } else if (id === "export") {
            mountInto(mounts.mountExportCenter, "export");
        } else if (id === "reviews") {
            mountInto(mounts.mountReviewEditor, "reviews");
        }
        activeTab = id;
    };
    let activeTab = "projects";
    showTab("projects");
    return root;
}

function createWorkspaceRoot() {
    const root = document.createElement("div");
    root.className = "manga-autopilot-root";
    root.style.padding = "12px";
    root.style.boxSizing = "border-box";
    root.style.maxWidth = "100%";
    root.style.overflowX = "hidden";
    root.style.fontFamily =
        "system-ui, -apple-system, 'Segoe UI', sans-serif";
    root.style.color = "var(--input-text, #ddd)";

    const heading = document.createElement("h2");
    heading.textContent = "Manga Autopilot";
    heading.style.marginTop = "0";
    heading.style.fontSize = "18px";
    root.appendChild(heading);

    root.appendChild(createWorkspaceView());
    return root;
}

function registerSidebarTab(extensionApp) {
    if (!extensionApp?.extensionManager?.registerSidebarTab) {
        return false;
    }
    extensionApp.extensionManager.registerSidebarTab({
        id: SIDEBAR_TAB_ID,
        icon: "pi pi-book",
        title: SIDEBAR_TAB_TITLE,
        tooltip: "Open the Manga Autopilot workspace",
        type: "custom",
        render: (element) => {
            element.replaceChildren(createWorkspaceRoot());
        },
    });
    return true;
}

export function getActiveProjectId() {
    return activeProjectId;
}

export function setActiveProjectId(projectId) {
    activeProjectId = projectId;
}

app.registerExtension({
    name: EXTENSION_NAME,
    async setup() {
        const ok = registerSidebarTab(app);
        if (!ok) {
            console.info(
                "[manga-autopilot] sidebar tab API not available; falling back to no-op.",
            );
        }
    },
});
