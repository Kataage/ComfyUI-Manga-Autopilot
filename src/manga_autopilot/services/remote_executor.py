"""Remote HTTP executor for external GPU workers (spec section 24.3 foundation).

This module provides :class:`RemoteHTTPExecutor`, a
:class:`GenerationExecutor` implementation that POSTs panel generation
requests to a remote HTTP worker and receives deterministic PNG bytes
in return.

The worker contract is intentionally simple for v0.1 — a synchronous
JSON request/response.  Future versions may add:

- Async job polling (``/v1/jobs/{id}``)
- Artifact URLs (S3 / R2 signed URLs) instead of base64
- Streaming / chunked transfers for large images
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from PIL import Image

from manga_autopilot.services.generation_job import (
    GenerationExecutor,
    GenerationExecutorResult,
)
from manga_autopilot.services.prompt_builder import PromptSpec

log = logging.getLogger(__name__)

# --------------------------------------------------------------- exceptions

class RemoteExecutorError(RuntimeError):
    """Base error raised by :class:`RemoteHTTPExecutor."""


class RemoteExecutorHTTPError(RemoteExecutorError):
    """Remote worker returned a non-200 HTTP response."""

    def __init__(self, status: int, body: str, url: str) -> None:
        self.status = status
        self.body = body
        self.url = url
        short_body = body[:200] if body else "(empty)"
        super().__init__(
            f"remote worker returned HTTP {status} from {url}: {short_body}"
        )


class RemoteExecutorTimeoutError(RemoteExecutorError):
    """Remote worker request timed out."""

    def __init__(self, timeout_sec: float, url: str) -> None:
        self.timeout_sec = timeout_sec
        self.url = url
        super().__init__(
            f"remote worker request timed out after {timeout_sec}s: {url}"
        )


class RemoteExecutorResponseError(RemoteExecutorError):
    """Remote worker returned a malformed or unsuccessful JSON response."""


class RemoteExecutorImageError(RemoteExecutorError):
    """Remote worker returned an invalid image payload."""


# --------------------------------------------------------------------- types

@dataclass
class RemoteWorkerSettings:
    """Connection settings for a remote GPU worker."""

    base_url: str = "http://127.0.0.1:9000"
    timeout_sec: float = 60.0
    api_key: str | None = None


@dataclass
class RemoteGenerateRequest:
    """Payload sent to ``POST /v1/generate-panel``."""

    project_id: str
    page_id: str
    panel_id: str
    prompt: str
    negative_prompt: str | None = None
    seed: int | None = None
    width: int = 512
    height: int = 512
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "project_id": self.project_id,
            "page_id": self.page_id,
            "panel_id": self.panel_id,
            "prompt": self.prompt,
            "width": self.width,
            "height": self.height,
            "metadata": self.metadata,
        }
        if self.negative_prompt is not None:
            d["negative_prompt"] = self.negative_prompt
        if self.seed is not None:
            d["seed"] = self.seed
        if self.workflow_id is not None:
            d["workflow_id"] = self.workflow_id
        return d


@dataclass
class RemoteGenerateResponse:
    """Response from ``POST /v1/generate-panel``."""

    status: str
    filename: str | None = None
    image_base64: str | None = None
    seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RemoteGenerateResponse:
        return cls(
            status=data.get("status", "error"),
            filename=data.get("filename"),
            image_base64=data.get("image_base64"),
            seed=data.get("seed"),
            metadata=data.get("metadata", {}),
            error=data.get("error"),
        )


# ---------------------------------------------------------------- executor

class RemoteHTTPExecutor(GenerationExecutor):
    """A :class:`GenerationExecutor` that delegates to a remote HTTP worker.

    The worker is expected to expose ``POST /v1/generate-panel`` which
    accepts a JSON payload and returns a JSON response with
    ``image_base64`` containing the rendered PNG.
    """

    def __init__(
        self,
        *,
        settings: RemoteWorkerSettings | None = None,
        project_id: str = "",
        session_factory: Any = None,
    ) -> None:
        self.settings = settings or RemoteWorkerSettings()
        self.project_id = project_id
        self._session_factory = session_factory

    def _open(self) -> aiohttp.ClientSession:
        if self._session_factory is not None:
            return self._session_factory()
        return aiohttp.ClientSession()

    async def submit(
        self,
        *,
        prompt: PromptSpec,
        workflow_id: str,
        seed: int,
        candidate_id: str,
    ) -> GenerationExecutorResult:
        request = RemoteGenerateRequest(
            project_id=self.project_id,
            page_id="",
            panel_id=candidate_id,
            prompt=prompt.positive,
            negative_prompt=prompt.negative or None,
            seed=seed,
            width=prompt.width,
            height=prompt.height,
            workflow_id=workflow_id or None,
        )
        url = self.settings.base_url.rstrip("/") + "/v1/generate-panel"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"

        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        session = self._open()
        try:
            try:
                async with session.post(
                    url, json=request.to_dict(), headers=headers, timeout=timeout,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise RemoteExecutorHTTPError(resp.status, body, url)
                    try:
                        data = await resp.json()
                    except (aiohttp.ContentTypeError, ValueError) as exc:
                        raise RemoteExecutorResponseError(
                            f"remote worker returned invalid JSON from {url}: {exc}"
                        ) from exc
            except asyncio.TimeoutError as exc:
                raise RemoteExecutorTimeoutError(
                    self.settings.timeout_sec, url
                ) from exc
        finally:
            if self._session_factory is None:
                await session.close()

        response = RemoteGenerateResponse.from_dict(data)
        if response.status != "completed":
            raise RemoteExecutorResponseError(
                f"remote worker returned status={response.status!r}: "
                f"{response.error or 'unknown error'}"
            )
        if not response.image_base64:
            raise RemoteExecutorResponseError("remote worker returned no image_base64")

        try:
            raw = base64.b64decode(response.image_base64)
        except Exception as exc:
            raise RemoteExecutorImageError(
                f"remote worker returned invalid base64: {exc}"
            ) from exc

        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as exc:
            raise RemoteExecutorImageError(
                f"remote worker returned invalid image bytes: {exc}"
            ) from exc

        return GenerationExecutorResult(
            candidate_id=candidate_id,
            prompt_id=response.metadata.get("prompt_id", f"remote_{candidate_id}"),
            image=image,
            workflow_id=workflow_id,
        )


# --------------------------------------------------------------- fake worker

class FakeRemoteWorker:
    """In-process aiohttp server that mimics a remote GPU worker.

    Used in tests so no real network or GPU is required.

    ``mode`` controls the response behaviour:

    - ``"success"`` — deterministic PNG response (default)
    - ``"http_500"`` — HTTP 500 with text body
    - ``"status_error"`` — JSON ``{"status": "error", ...}``
    - ``"invalid_json"`` — non-JSON text response
    - ``"missing_image"`` — JSON without ``image_base64``
    - ``"invalid_base64"`` — JSON with non-base64 ``image_base64``
    - ``"invalid_image"`` — valid base64 but not a valid image
    - ``"timeout"`` — sleeps for ``delay_sec`` before responding
    """

    def __init__(
        self,
        *,
        mode: str = "success",
        seed: int = 42,
        delay_sec: float = 0.0,
    ) -> None:
        self.mode = mode
        self.seed = seed
        self.delay_sec = delay_sec
        self.requests: list[dict[str, Any]] = []
        self.headers: list[dict[str, str | Any]] = []

    async def handle_generate(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))

        # --- timeout mode ---
        if self.mode == "timeout" and self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
            # After sleep, return error (client should have timed out).
            return aiohttp.web.json_response(
                {"status": "error", "error": "delayed response"},
                status=200,
            )

        # --- HTTP 500 ---
        if self.mode == "http_500":
            return aiohttp.web.Response(
                status=500,
                text="internal server error",
                content_type="text/plain",
            )

        # --- status error ---
        if self.mode == "status_error":
            return aiohttp.web.json_response(
                {"status": "error", "error": "model not found"},
            )

        # --- invalid JSON ---
        if self.mode == "invalid_json":
            return aiohttp.web.Response(
                status=200,
                text="this is not json {{{",
                content_type="application/json",
            )

        width = int(body.get("width", 64))
        height = int(body.get("height", 64))
        seed = int(body.get("seed", self.seed))

        # --- missing image_base64 ---
        if self.mode == "missing_image":
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{body.get('panel_id', 'panel')}.png",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- invalid base64 ---
        if self.mode == "invalid_base64":
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{body.get('panel_id', 'panel')}.png",
                "image_base64": "this-is-not-base64!!!",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- invalid image bytes (valid base64, not a valid image) ---
        if self.mode == "invalid_image":
            import base64 as _b64
            fake_bytes = _b64.b64encode(b"not-an-image").decode("ascii")
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{body.get('panel_id', 'panel')}.png",
                "image_base64": fake_bytes,
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- success (default) ---
        r = (seed * 7) % 256
        g = (seed * 13) % 256
        b = (seed * 23) % 256
        img = Image.new("RGB", (width, height), (r, g, b))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return aiohttp.web.json_response({
            "status": "completed",
            "filename": f"{body.get('panel_id', 'panel')}.png",
            "image_base64": b64,
            "seed": seed,
            "metadata": {
                "executor": "fake-remote",
                "prompt_id": f"fake_{body.get('panel_id', 'unknown')}",
            },
        })

    def app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        app.router.add_post("/v1/generate-panel", self.handle_generate)
        return app


__all__ = [
    "RemoteExecutorError",
    "RemoteExecutorHTTPError",
    "RemoteExecutorTimeoutError",
    "RemoteExecutorResponseError",
    "RemoteExecutorImageError",
    "RemoteWorkerSettings",
    "RemoteGenerateRequest",
    "RemoteGenerateResponse",
    "RemoteHTTPExecutor",
    "FakeRemoteWorker",
]
