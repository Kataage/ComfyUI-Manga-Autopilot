/**
 * ComfyUI Manga Autopilot web extension entry point.
 *
 * Registers a sidebar tab and topbar menu entry against the ComfyUI client so
 * users can find the Manga Autopilot workspace.  The actual screens are
 * populated by subsequent issues; this file only wires the extension into
 * ComfyUI's lifecycle.
 *
 * Spec reference: comfyui_manga_autopilot_spec.md section 11.
 */

import { app } from "../../scripts/app.js";

export const EXTENSION_NAME = "comfyui.manga.autopilot";
export const SIDEBAR_TAB_ID = "manga-autopilot";
export const SIDEBAR_TAB_TITLE = "Manga Autopilot";

function createPlaceholderRoot() {
    const root = document.createElement("div");
    root.className = "manga-autopilot-root";
    root.style.padding = "12px";
    root.style.fontFamily =
        "system-ui, -apple-system, 'Segoe UI', sans-serif";
    root.style.color = "var(--input-text, #ddd)";

    const heading = document.createElement("h2");
    heading.textContent = "Manga Autopilot";
    heading.style.marginTop = "0";
    heading.style.fontSize = "18px";

    const message = document.createElement("p");
    message.textContent =
        "UI is under construction. The backend API is exposed under /manga_autopilot/api.";
    message.style.fontSize = "13px";
    message.style.opacity = "0.85";

    root.appendChild(heading);
    root.appendChild(message);
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
            element.replaceChildren(createPlaceholderRoot());
        },
    });
    return true;
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
