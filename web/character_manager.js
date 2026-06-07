// Character Manager UI (spec section 22).
// Provides a vanilla-JS panel that lists, creates, updates, and deletes
// characters via the HTTP API at /manga_autopilot/api/projects/{id}/characters.

(function () {
  "use strict";

  const EXPRESSIONS = [
    "neutral", "smile", "angry", "sad", "crying", "surprised", "determined",
    "embarrassed", "fear", "pain", "shouting", "relieved", "confused",
    "serious", "despair",
  ];
  const POSES = [
    "standing", "running", "walking", "falling", "kneeling", "looking back",
    "holding sword", "battle stance", "reaching hand", "turning around",
    "close-up face", "upper body shot", "from behind", "low angle", "high angle",
  ];
  const ROLES = ["protagonist", "heroine", "villain", "support", "mob"];

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") node.className = v;
      else if (k === "style") node.style.cssText = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (k === "value" || k === "checked" || k === "selected" || k === "disabled") {
        node[k] = v;
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
      return r.status === 204 ? null : r.json();
    });
  }

  function CharacterForm({ character, onSubmit, onCancel }) {
    const isEdit = !!character;
    const data = character
      ? JSON.parse(JSON.stringify(character))
      : {
          id: "",
          name: "",
          role: "support",
          description: "",
          appearance: {
            hair_color: "",
            hair_style: "",
            eye_color: "",
            face_features: [],
            distinctive_features: [],
          },
          outfit: { base: "", must_keep: [], must_avoid: [] },
          color_palette: { primary: "#000000" },
          consistency_prompt: "",
        };

    const idInput = el("input", { value: data.id, placeholder: "alice", disabled: isEdit });
    const nameInput = el("input", { value: data.name, placeholder: "Alice" });
    const roleSelect = el(
      "select",
      {},
      ...ROLES.map((r) =>
        el("option", { value: r, selected: r === data.role ? "selected" : null }, r)
      )
    );
    const descInput = el("textarea", { rows: 2 }, data.description);
    const hairColor = el("input", { value: data.appearance.hair_color });
    const hairStyle = el("input", { value: data.appearance.hair_style });
    const eyeColor = el("input", { value: data.appearance.eye_color });
    const mustKeep = el("textarea", { rows: 2 }, (data.outfit.must_keep || []).join("\n"));
    const mustAvoid = el("textarea", { rows: 2 }, (data.outfit.must_avoid || []).join("\n"));
    const primary = el("input", { type: "color", value: data.color_palette.primary || "#000000" });
    const consistency = el("textarea", { rows: 2 }, data.consistency_prompt || "");

    return el(
      "form",
      {
        class: "manga-char-form",
        onSubmit: (ev) => {
          ev.preventDefault();
          const payload = {
            id: idInput.value.trim(),
            name: nameInput.value.trim(),
            role: roleSelect.value,
            description: descInput.value,
            appearance: {
              hair_color: hairColor.value,
              hair_style: hairStyle.value,
              eye_color: eyeColor.value,
            },
            outfit: {
              base: "",
              must_keep: mustKeep.value.split(/\n+/).map((s) => s.trim()).filter(Boolean),
              must_avoid: mustAvoid.value.split(/\n+/).map((s) => s.trim()).filter(Boolean),
            },
            color_palette: { primary: primary.value },
            consistency_prompt: consistency.value,
          };
          onSubmit(payload);
        },
      },
      el("label", {}, "ID (slug)"),
      idInput,
      el("label", {}, "Name"),
      nameInput,
      el("label", {}, "Role"),
      roleSelect,
      el("label", {}, "Description"),
      descInput,
      el("label", {}, "Hair color"),
      hairColor,
      el("label", {}, "Hair style"),
      hairStyle,
      el("label", {}, "Eye color"),
      eyeColor,
      el("label", {}, "mustKeep (one per line)"),
      mustKeep,
      el("label", {}, "mustAvoid (one per line)"),
      mustAvoid,
      el("label", {}, "Primary color"),
      primary,
      el("label", {}, "Consistency prompt"),
      consistency,
      el("div", { class: "manga-char-actions" },
        el("button", { type: "submit" }, isEdit ? "Save" : "Create"),
        onCancel ? el("button", { type: "button", onClick: onCancel }, "Cancel") : null
      )
    );
  }

  function ReferenceUploader({ projectId, characterId, onUploaded }) {
    const fileInput = el("input", { type: "file", accept: "image/*" });
    const labelInput = el("input", { placeholder: "label (optional)" });
    const uploadBtn = el(
      "button",
      {
        type: "button",
        onClick: async () => {
          const file = fileInput.files[0];
          if (!file) return;
          const buf = new Uint8Array(await file.arrayBuffer());
          let binary = "";
          for (let i = 0; i < buf.length; i++) binary += String.fromCharCode(buf[i]);
          const data_base64 = btoa(binary);
          const body = {
            filename: file.name,
            label: labelInput.value || "",
            data_base64,
          };
          const path = `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(characterId)}/references`;
          await api(path, { method: "POST", body: JSON.stringify(body) });
          if (onUploaded) onUploaded();
        },
      },
      "Upload"
    );
    return el(
      "div",
      { class: "manga-ref-uploader" },
      fileInput,
      labelInput,
      uploadBtn
    );
  }

  function CharacterList({ characters, onEdit, onDelete, onRefresh }) {
    return el(
      "div",
      { class: "manga-char-list" },
      ...characters.map((c) =>
        el(
          "div",
          { class: "manga-char-row" },
          el("strong", {}, c.name),
          el("span", { class: "manga-char-id" }, c.id),
          el("span", { class: "manga-char-role" }, c.role),
          el(
            "div",
            { class: "manga-char-actions" },
            el("button", { type: "button", onClick: () => onEdit(c) }, "Edit"),
            el(
              "button",
              {
                type: "button",
                onClick: async () => {
                  if (!confirm(`Delete character ${c.id}?`)) return;
                  await onDelete(c.id);
                  onRefresh();
                },
              },
              "Delete"
            )
          )
        )
      )
    );
  }

  function CharacterManager(root, opts = {}) {
    const projectId = opts.projectId;
    if (!projectId) {
      root.appendChild(el("div", { class: "manga-err" }, "projectId is required"));
      return;
    }
    const listEl = el("div", { class: "manga-char-list-wrap" });
    const formHost = el("div", { class: "manga-char-form-host" });
    const uploadHost = el("div", { class: "manga-char-upload-host" });
    const newBtn = el(
      "button",
      {
        type: "button",
        onClick: () => {
          formHost.replaceChildren(
            CharacterForm({
              onSubmit: async (payload) => {
                await api(
                  `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/characters`,
                  { method: "POST", body: JSON.stringify(payload) }
                );
                formHost.replaceChildren();
                await refresh();
              },
              onCancel: () => formHost.replaceChildren(),
            })
          );
        },
      },
      "+ New character"
    );
    const refresh = async () => {
      const chars = await api(
        `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/characters`
      );
      listEl.replaceChildren(
        CharacterList({
          characters: chars,
          onEdit: (char) => {
            formHost.replaceChildren(
              CharacterForm({
                character: char,
                onSubmit: async (payload) => {
                  const id = char.id;
                  await api(
                    `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(id)}`,
                    { method: "PUT", body: JSON.stringify(payload) }
                  );
                  formHost.replaceChildren();
                  await refresh();
                },
                onCancel: () => formHost.replaceChildren(),
              })
            );
            uploadHost.replaceChildren(
              ReferenceUploader({
                projectId,
                characterId: char.id,
                onUploaded: () => refresh(),
              })
            );
          },
          onDelete: async (id) =>
            api(
              `/manga_autopilot/api/projects/${encodeURIComponent(projectId)}/characters/${encodeURIComponent(id)}`,
              { method: "DELETE" }
            ),
          onRefresh: refresh,
        })
      );
    };
    root.appendChild(el("div", { class: "manga-char-toolbar" }, newBtn));
    root.appendChild(listEl);
    root.appendChild(formHost);
    root.appendChild(uploadHost);
    refresh().catch((e) => root.appendChild(el("div", { class: "manga-err" }, e.message)));
  }

  window.MangaAutopilot = window.MangaAutopilot || {};
  window.MangaAutopilot.mountCharacterManager = CharacterManager;
  window.MangaAutopilot.characterPresets = { EXPRESSIONS, POSES, ROLES };
})();
