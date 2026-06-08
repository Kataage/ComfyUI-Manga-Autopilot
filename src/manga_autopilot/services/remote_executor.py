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
            async with session.post(
                url, json=request.to_dict(), headers=headers, timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(
                        f"remote worker returned {resp.status}: {body}"
                    )
                data = await resp.json()
        finally:
            if self._session_factory is None:
                await session.close()

        response = RemoteGenerateResponse.from_dict(data)
        if response.status != "completed":
            raise RuntimeError(
                f"remote worker returned status={response.status!r}: "
                f"{response.error or 'unknown error'}"
            )
        if not response.image_base64:
            raise RuntimeError("remote worker returned no image_base64")

        raw = base64.b64decode(response.image_base64)
        image = Image.open(io.BytesIO(raw))
        image.load()

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
    """

    def __init__(self, *, seed: int = 42) -> None:
        self.seed = seed
        self.requests: list[dict[str, Any]] = []

    async def handle_generate(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        body = await request.json()
        self.requests.append(body)

        width = int(body.get("width", 64))
        height = int(body.get("height", 64))
        seed = int(body.get("seed", self.seed))

        # Deterministic: colour derived from seed.
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
    "RemoteWorkerSettings",
    "RemoteGenerateRequest",
    "RemoteGenerateResponse",
    "RemoteHTTPExecutor",
    "FakeRemoteWorker",
]
