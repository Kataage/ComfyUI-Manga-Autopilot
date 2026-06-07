"""Async client for the ComfyUI Server API.

Spec reference: ``docs/comfyui_manga_autopilot_spec.md`` section 10.

This module only contains the *transport* primitives: connection settings,
session lifecycle, request helpers, and typed exceptions.  Each high-level
ComfyUI endpoint (``/prompt``, ``/history/...``, ``/view``, ...) gets its own
issue and lives in a focused method on top of this client.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8188"
DEFAULT_TIMEOUT_SEC = 600
DEFAULT_CLIENT_ID = "manga_autopilot_client"


class ComfyUIError(RuntimeError):
    """Base class for ComfyUI client errors."""


class ComfyUIRequestError(ComfyUIError):
    """Raised when a request fails or returns a non-2xx status."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ComfyClient:
    """Lightweight async wrapper around the ComfyUI Server API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        client_id: str = DEFAULT_CLIENT_ID,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.client_id = client_id
        self._session = session
        self._owns_session = session is None

    # --------------------------------------------------------------- session
    async def __aenter__(self) -> ComfyClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session and self._owns_session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ URLs
    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    # --------------------------------------------------------- low-level ops
    async def get_json(self, path: str, **params: Any) -> Any:
        session = await self._ensure_session()
        async with session.get(self.url(path), params=params or None) as resp:
            return await self._read_json(resp)

    async def get_bytes(self, path: str, **params: Any) -> bytes:
        session = await self._ensure_session()
        async with session.get(self.url(path), params=params or None) as resp:
            if resp.status >= 400:
                body = await resp.text()
                raise ComfyUIRequestError(
                    f"GET {path} failed ({resp.status})",
                    status=resp.status,
                    body=body,
                )
            return await resp.read()

    async def post_json(self, path: str, payload: Any) -> Any:
        session = await self._ensure_session()
        async with session.post(self.url(path), json=payload) as resp:
            return await self._read_json(resp)

    async def post_multipart(self, path: str, fields: dict[str, Any]) -> Any:
        session = await self._ensure_session()
        form = aiohttp.FormData()
        for key, value in fields.items():
            if isinstance(value, tuple):
                form.add_field(key, value[1], filename=value[0], content_type=value[2])
            else:
                form.add_field(key, value)
        async with session.post(self.url(path), data=form) as resp:
            return await self._read_json(resp)

    @staticmethod
    async def _read_json(resp: aiohttp.ClientResponse) -> Any:
        text = await resp.text()
        if resp.status >= 400:
            raise ComfyUIRequestError(
                f"{resp.method} {resp.url.path} failed ({resp.status})",
                status=resp.status,
                body=text,
            )
        if not text:
            return None
        try:
            return await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as exc:
            raise ComfyUIRequestError(
                f"Non-JSON response from {resp.url.path}: {text!r}",
                status=resp.status,
                body=text,
            ) from exc

    # ----------------------------------------------------------- /prompt API
    async def submit_workflow(
        self,
        workflow: dict[str, Any],
        *,
        client_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
    ) -> str:
        """Submit an API-format workflow and return the assigned ``prompt_id``.

        ``workflow`` is the bare ``{node_id: {class_type, inputs}}`` mapping --
        the same shape ComfyUI's ``/prompt`` endpoint expects under the
        ``prompt`` key.  The wrapper attaches ``client_id`` (defaulting to
        ``self.client_id``) and optional ``extra_data``.
        """

        if not isinstance(workflow, dict) or not workflow:
            raise ValueError("workflow must be a non-empty mapping of node ids")

        payload: dict[str, Any] = {
            "prompt": workflow,
            "client_id": client_id or self.client_id,
        }
        if extra_data:
            payload["extra_data"] = extra_data

        response = await self.post_json("/prompt", payload)
        if not isinstance(response, dict):
            raise ComfyUIRequestError(
                f"Unexpected /prompt response (not an object): {response!r}"
            )

        if "error" in response and response.get("error"):
            raise ComfyUIRequestError(
                f"/prompt rejected: {response.get('error')}",
                body=str(response),
            )

        prompt_id = response.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIRequestError(
                f"/prompt response missing prompt_id: {response!r}",
                body=str(response),
            )
        return prompt_id

    async def get_queue_state(self) -> dict[str, Any]:
        """Return ComfyUI's current queue state (running + pending)."""

        body = await self.get_json("/queue")
        return body if isinstance(body, dict) else {"queue": body}

    # ------------------------------------------------------------- /ws API
    def _ws_url(self, client_id: str | None = None) -> str:
        cid = client_id or self.client_id
        parsed = urlparse(self.base_url)
        ws_scheme = "wss" if parsed.scheme == "https" else "ws"
        ws_base = urlunparse((ws_scheme, parsed.netloc, "/ws", "", "", ""))
        return f"{ws_base}?clientId={cid}"

    async def listen_events(
        self,
        *,
        client_id: str | None = None,
        max_reconnects: int = 3,
        reconnect_delay_sec: float = 1.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield ComfyUI WebSocket events forever.

        The iterator transparently reconnects up to ``max_reconnects`` times on
        dropped connections.  Each event is a parsed JSON dict; non-JSON
        frames (binary previews) are skipped.

        Spec reference: section 10.6.
        """

        attempts = 0
        url = self._ws_url(client_id=client_id)

        while True:
            session = await self._ensure_session()
            try:
                async with session.ws_connect(url, heartbeat=30) as ws:
                    attempts = 0  # reset on successful connect
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                payload = json.loads(msg.data)
                            except json.JSONDecodeError:
                                log.debug("Skipping non-JSON ws frame")
                                continue
                            if isinstance(payload, dict):
                                yield payload
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            # ComfyUI uses binary frames for preview thumbnails.
                            continue
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                        ):
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            log.warning("ws error: %s", ws.exception())
                            break
            except aiohttp.ClientError as exc:
                log.warning("ComfyUI ws connection failed: %s", exc)
            except asyncio.CancelledError:
                raise

            attempts += 1
            if attempts > max_reconnects:
                log.info("ws reconnect limit reached (%s); stopping.", max_reconnects)
                return
            await asyncio.sleep(reconnect_delay_sec)


__all__ = [
    "ComfyClient",
    "ComfyUIError",
    "ComfyUIRequestError",
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_TIMEOUT_SEC",
]
