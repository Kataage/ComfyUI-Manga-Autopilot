"""Optional ComfyUI integration glue.

When the package is imported inside a running ComfyUI process, this module is
responsible for binding our aiohttp routes onto the ``PromptServer`` singleton
and configuring the application context (storage_root, workflow registry) so
the route handlers can find their dependencies.

Outside of ComfyUI (during pytest, standalone runs, etc.) the import is a
no-op so the rest of the package stays testable.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _resolve_app_and_routes(server_obj: object) -> tuple[object | None, object | None]:
    """Return (application, router) for a ComfyUI PromptServer-like object.

    Newer ComfyUI exposes ``.app`` (aiohttp Application). Older versions only
    have ``.routes`` (UrlDispatcher). We try both, and fall back to walking
    the attribute graph if neither matches.
    """

    app = getattr(server_obj, "app", None)
    routes = getattr(server_obj, "routes", None)
    if app is None and routes is not None and hasattr(routes, "_app"):
        app = routes._app
    return app, routes


def _default_storage_root() -> Path:
    """Pick a durable on-disk storage root for this ComfyUI process.

    Delegates to :func:`manga_autopilot.default_storage_root` which resolves
    the user data directory using the documented precedence rules.
    """

    from manga_autopilot import default_storage_root

    return default_storage_root()


def _repo_root() -> Path:
    """Return the extension's own root (the directory holding ``__init__.py``).

    ``comfy_integration.py`` lives at ``<repo>/src/manga_autopilot/``, so the
    repo root is two levels up from this file's parent package.
    """

    return Path(__file__).resolve().parent.parent.parent


def _default_config_path() -> Path | None:
    """Find a user-authored ``config.yaml`` next to the extension, if any.

    ``$MANGA_AUTOPILOT_CONFIG_PATH`` overrides the search when set (an empty
    value is treated the same as "not set" so callers can safely export it
    unconditionally).
    """

    from manga_autopilot.config import discover_config_path

    override = os.environ.get("MANGA_AUTOPILOT_CONFIG_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return discover_config_path(_repo_root())


def _build_llm_provider(config_path: Path | None):
    """Build the LLM provider ``config.yaml`` (or its absence) describes.

    Mirrors ``workflow_routes._comfy_client``'s lazy-default idiom: if no
    ``config.yaml`` is found, ``load_config(None)`` already returns
    :class:`~manga_autopilot.config.AppConfig`'s built-in default (Ollama at
    its default port), so the caller still gets a real, usable provider
    rather than the silent ``ManualProvider`` no-op that
    ``routes.autopilot_routes._llm_provider`` falls back to when nothing is
    configured at all.

    ``config.py``'s ``LLMSettings`` (the user-facing ``config.yaml`` schema)
    and ``services.llm_provider``'s ``LLMSettings`` (what actually builds a
    provider) are two separate models with different field names
    (``provider`` vs ``type``); this is the one place that bridges them.
    """

    from manga_autopilot.config import load_config
    from manga_autopilot.services.llm_provider import LLMSettings as ProviderSettings
    from manga_autopilot.services.llm_provider import build_provider

    cfg = load_config(config_path)
    provider_type = cfg.llm.provider.strip().lower()
    if provider_type == "lm_studio":
        # A natural spelling for an OpenAI-compatible LM Studio endpoint;
        # services.llm_provider only knows the generic "openai_compatible".
        provider_type = "openai_compatible"
    settings = ProviderSettings(
        type=provider_type,  # type: ignore[arg-type]
        endpoint=cfg.llm.endpoint,
        model=cfg.llm.model,
        temperature=cfg.llm.temperature,
        max_tokens=cfg.llm.max_tokens,
        timeout_sec=cfg.llm.timeout_sec,
    )
    return build_provider(settings)


def attach_routes_to_prompt_server() -> bool:
    """Attach Manga Autopilot routes + context to ComfyUI's PromptServer.

    Returns ``True`` if the integration succeeded, ``False`` otherwise. This
    function never raises; failures are logged at INFO level because they are
    expected outside ComfyUI (for example during tests).
    """

    try:
        from server import PromptServer  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - exercised only inside ComfyUI
        log.info("PromptServer not available; skipping route attachment.")
        return False

    try:
        server = PromptServer.instance
    except Exception:  # pragma: no cover
        log.exception("Failed to access PromptServer.instance")
        return False

    app, routes = _resolve_app_and_routes(server)
    if app is None and routes is None:
        log.warning("PromptServer has neither .app nor .routes; skipping attachment.")
        return False

    from manga_autopilot.routes import register_all

    storage_root = _default_storage_root()
    try:
        if app is not None:
            register_all(app, storage_root=str(storage_root))
        else:
            register_all(routes, storage_root=str(storage_root))
    except Exception:  # pragma: no cover
        log.exception("Failed to register Manga Autopilot routes")
        return False

    if app is not None and app.get("manga_llm_provider") is None:
        # ``register_all`` never wires a real LLM provider: without this,
        # ``autopilot_routes._llm_provider`` falls back to ``ManualProvider``,
        # a silent no-op that returns "{}" for every planning call. That
        # fallback exists for tests, not for a live install, so give a real
        # install a real provider built from ``config.yaml`` (or its
        # documented Ollama-at-defaults fallback) here, once, at startup.
        try:
            app["manga_llm_provider"] = _build_llm_provider(_default_config_path())
        except Exception:  # pragma: no cover - keep startup non-fatal
            log.exception("Failed to build LLM provider from config.yaml; "
                          "planning calls will use the manual no-op provider.")

    log.info(
        "Manga Autopilot routes attached to PromptServer (storage_root=%s).",
        storage_root,
    )
    return True


__all__ = ["attach_routes_to_prompt_server"]
