"""Remote HTTP executor for external GPU workers (spec section 24.3 foundation).

This module provides :class:`RemoteHTTPExecutor`, a
:class:`GenerationExecutor` implementation that POSTs panel generation
requests to a remote HTTP worker and receives deterministic PNG bytes
in return.

The worker supports two modes:

- **Synchronous**: ``POST /v1/generate-panel`` returns ``completed``
  with ``image_base64`` in a single response.
- **Asynchronous**: ``POST`` returns ``queued`` or ``accepted`` with a
  ``job_id``; the executor polls ``GET /v1/jobs/{job_id}`` until the
  job reaches ``completed`` or ``error``.

Future versions may add:

- Artifact URLs (S3 / R2 signed URLs) instead of base64
- Streaming / chunked transfers for large images
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
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


class RemoteExecutorPollingTimeoutError(RemoteExecutorTimeoutError):
    """Remote worker job did not complete within polling timeout."""

    def __init__(self, job_id: str, timeout_sec: float, url: str) -> None:
        self.job_id = job_id
        super().__init__(timeout_sec, url)
        self.args = (
            f"job {job_id!r} did not complete within {timeout_sec}s polling: {url}",
        )


class RemoteExecutorJobError(RemoteExecutorResponseError):
    """Remote worker job reached error state."""

    def __init__(self, job_id: str, error: str) -> None:
        self.job_id = job_id
        self.job_error = error
        super().__init__(f"job {job_id!r} failed: {error}")


# --------------------------------------------------------------------- types

@dataclass
class RemoteWorkerSettings:
    """Connection settings for a remote GPU worker."""

    base_url: str = "http://127.0.0.1:9000"
    timeout_sec: float = 60.0
    api_key: str | None = None
    poll_interval_sec: float = 0.1
    poll_timeout_sec: float = 60.0
    max_poll_attempts: int | None = None


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
    job_id: str | None = None
    filename: str | None = None
    image_base64: str | None = None
    artifact_url: str | None = None
    artifact_path: str | None = None
    seed: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RemoteGenerateResponse:
        return cls(
            status=data.get("status", "error"),
            job_id=data.get("job_id"),
            filename=data.get("filename"),
            image_base64=data.get("image_base64"),
            artifact_url=data.get("artifact_url"),
            artifact_path=data.get("artifact_path"),
            seed=data.get("seed"),
            metadata=data.get("metadata", {}),
            error=data.get("error"),
        )


# ---------------------------------------------------------------- executor

class RemoteHTTPExecutor(GenerationExecutor):
    """A :class:`GenerationExecutor` that delegates to a remote HTTP worker.

    The worker is expected to expose ``POST /v1/generate-panel`` which
    accepts a JSON payload and returns a JSON response.

    **Synchronous mode**: the response contains ``image_base64`` directly.

    **Asynchronous mode**: the response contains ``job_id`` with status
    ``queued`` / ``accepted`` / ``running``.  The executor then polls
    ``GET /v1/jobs/{job_id}`` until the job completes or errors.
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

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    async def _fetch_json(self, session: aiohttp.ClientSession, url: str) -> dict[str, Any]:
        """GET a URL and return parsed JSON.  Raises on HTTP or parse errors."""
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        try:
            async with session.get(url, headers=self._auth_headers(), timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RemoteExecutorHTTPError(resp.status, body, url)
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise RemoteExecutorResponseError(
                        f"remote worker returned invalid JSON from {url}: {exc}"
                    ) from exc
        except asyncio.TimeoutError as exc:
            raise RemoteExecutorTimeoutError(self.settings.timeout_sec, url) from exc

    def _decode_image(self, image_base64: str, candidate_id: str) -> Image.Image:
        """Decode base64 PNG into a PIL Image."""
        try:
            raw = base64.b64decode(image_base64)
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
        return image

    async def _fetch_image_bytes(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> bytes:
        """HTTP GET an image URL and return raw bytes."""
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        try:
            async with session.get(url, headers=self._auth_headers(), timeout=timeout) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RemoteExecutorHTTPError(resp.status, body, url)
                return await resp.read()
        except asyncio.TimeoutError as exc:
            raise RemoteExecutorTimeoutError(self.settings.timeout_sec, url) from exc

    def _load_image_from_path(self, path: str) -> Image.Image:
        """Read a local image file into a PIL Image."""
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            raise RemoteExecutorImageError(
                f"artifact_path does not exist: {path}"
            )
        try:
            image = Image.open(p)
            image.load()
        except Exception as exc:
            raise RemoteExecutorImageError(
                f"artifact_path is not a valid image: {path}: {exc}"
            ) from exc
        return image

    def _resolve_image_from_response(
        self,
        response: RemoteGenerateResponse,
        session: aiohttp.ClientSession,
        candidate_id: str,
    ) -> Image.Image:
        """Resolve image from response using priority: base64 > artifact_url > artifact_path.

        artifact_url requires an async session, so this is a sync fallback
        for base64 and artifact_path only.  artifact_url fetching is done
        in ``_resolve_artifact_url``.
        """
        if response.image_base64:
            return self._decode_image(response.image_base64, candidate_id)

        if response.artifact_path:
            return self._load_image_from_path(response.artifact_path)

        raise RemoteExecutorResponseError(
            "remote worker returned no image_base64, artifact_url, or artifact_path"
        )

    async def _resolve_artifact_url(
        self,
        session: aiohttp.ClientSession,
        artifact_url: str,
        candidate_id: str,
    ) -> Image.Image:
        """Download image from artifact_url."""
        raw = await self._fetch_image_bytes(session, artifact_url)
        try:
            image = Image.open(io.BytesIO(raw))
            image.load()
        except Exception as exc:
            raise RemoteExecutorImageError(
                f"artifact_url returned invalid image bytes: {exc}"
            ) from exc
        return image

    async def _poll_job(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        candidate_id: str,
        workflow_id: str,
    ) -> GenerationExecutorResult:
        """Poll ``GET /v1/jobs/{job_id}`` until completed or error."""
        base = self.settings.base_url.rstrip("/")
        job_url = f"{base}/v1/jobs/{job_id}"
        deadline = asyncio.get_event_loop().time() + self.settings.poll_timeout_sec
        attempts = 0

        while True:
            attempts += 1
            if self.settings.max_poll_attempts is not None:
                if attempts > self.settings.max_poll_attempts:
                    raise RemoteExecutorPollingTimeoutError(
                        job_id, self.settings.poll_timeout_sec, job_url,
                    )

            data = await self._fetch_json(session, job_url)
            status = data.get("status", "error")

            if status == "completed":
                resp = RemoteGenerateResponse.from_dict(data)

                # Priority: image_base64 > artifact_url > artifact_path
                if resp.image_base64:
                    image = self._decode_image(resp.image_base64, candidate_id)
                elif resp.artifact_url:
                    image = await self._resolve_artifact_url(
                        session, resp.artifact_url, candidate_id,
                    )
                elif resp.artifact_path:
                    image = self._load_image_from_path(resp.artifact_path)
                else:
                    raise RemoteExecutorResponseError(
                        f"job {job_id!r} completed but no image_base64, "
                        f"artifact_url, or artifact_path"
                    )

                return GenerationExecutorResult(
                    candidate_id=candidate_id,
                    prompt_id=data.get("metadata", {}).get(
                        "prompt_id", f"remote_{candidate_id}"
                    ),
                    image=image,
                    workflow_id=workflow_id,
                )

            if status == "error":
                raise RemoteExecutorJobError(
                    job_id, data.get("error", "unknown error")
                )

            # queued / running — check timeout and retry
            if asyncio.get_event_loop().time() >= deadline:
                raise RemoteExecutorPollingTimeoutError(
                    job_id, self.settings.poll_timeout_sec, job_url,
                )

            await asyncio.sleep(self.settings.poll_interval_sec)

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
        timeout = aiohttp.ClientTimeout(total=self.settings.timeout_sec)
        session = self._open()
        try:
            try:
                async with session.post(
                    url, json=request.to_dict(), headers=self._auth_headers(), timeout=timeout,
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

        # --- synchronous completed ---
        if response.status == "completed":
            # Priority: image_base64 > artifact_url > artifact_path
            if response.image_base64:
                image = self._decode_image(response.image_base64, candidate_id)
            elif response.artifact_url:
                dl_session = self._open()
                try:
                    image = await self._resolve_artifact_url(
                        dl_session, response.artifact_url, candidate_id,
                    )
                finally:
                    if self._session_factory is None:
                        await dl_session.close()
            elif response.artifact_path:
                image = self._load_image_from_path(response.artifact_path)
            else:
                raise RemoteExecutorResponseError(
                    "remote worker returned no image_base64, artifact_url, or artifact_path"
                )

            return GenerationExecutorResult(
                candidate_id=candidate_id,
                prompt_id=response.metadata.get("prompt_id", f"remote_{candidate_id}"),
                image=image,
                workflow_id=workflow_id,
            )

        # --- async: queued / accepted / running with job_id ---
        if response.status in ("queued", "accepted", "running") and response.job_id:
            session_for_poll = self._open()
            try:
                return await self._poll_job(
                    session_for_poll, response.job_id, candidate_id, workflow_id,
                )
            finally:
                if self._session_factory is None:
                    await session_for_poll.close()

        # --- anything else is an error ---
        raise RemoteExecutorResponseError(
            f"remote worker returned status={response.status!r}: "
            f"{response.error or 'unknown error'}"
        )


# --------------------------------------------------------------- fake worker

class FakeRemoteWorker:
    """In-process aiohttp server that mimics a remote GPU worker.

    Used in tests so no real network or GPU is required.

    ``mode`` controls the response behaviour:

    **Synchronous modes:**

    - ``"success"`` — deterministic PNG response (default)
    - ``"http_500"`` — HTTP 500 with text body
    - ``"status_error"`` — JSON ``{"status": "error", ...}``
    - ``"invalid_json"`` — non-JSON text response
    - ``"missing_image"`` — JSON without ``image_base64``
    - ``"invalid_base64"`` — JSON with non-base64 ``image_base64``
    - ``"invalid_image"`` — valid base64 but not a valid image
    - ``"timeout"`` — sleeps for ``delay_sec`` before responding

    **Asynchronous modes:**

    - ``"async_success"`` — queued → running → completed
    - ``"async_error"`` — queued → error
    - ``"async_timeout"`` — always returns running (never completes)

    **Artifact modes:**

    - ``"artifact_url"`` — completed with artifact_url
    - ``"artifact_path"`` — completed with artifact_path
    - ``"async_artifact_url"`` — queued → completed with artifact_url
    - ``"async_artifact_path"`` — queued → completed with artifact_path
    - ``"artifact_url_404"`` — completed with artifact_url that returns 404
    - ``"artifact_path_missing"`` — completed with artifact_path to nonexistent file
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
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_count: dict[str, int] = {}
        self.job_requests: list[dict[str, Any]] = []
        self.artifacts: dict[str, bytes] = {}
        self._artifact_tmp_dir: str | None = None
        self._server_port: int = 0

    def _make_image_b64(self, width: int, height: int, seed: int) -> str:
        r = (seed * 7) % 256
        g = (seed * 13) % 256
        b = (seed * 23) % 256
        img = Image.new("RGB", (width, height), (r, g, b))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def _make_image_bytes(self, width: int, height: int, seed: int) -> bytes:
        r = (seed * 7) % 256
        g = (seed * 13) % 256
        b = (seed * 23) % 256
        img = Image.new("RGB", (width, height), (r, g, b))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def _store_artifact(self, filename: str, image_bytes: bytes) -> None:
        self.artifacts[filename] = image_bytes

    def _get_tmp_dir(self) -> str:
        if self._artifact_tmp_dir is None:
            import tempfile
            self._artifact_tmp_dir = tempfile.mkdtemp(prefix="fake_worker_")
        return self._artifact_tmp_dir

    # ---- POST /v1/generate-panel ----

    async def handle_generate(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))

        # --- timeout mode ---
        if self.mode == "timeout" and self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)
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
        panel_id = body.get("panel_id", "panel")

        # --- async modes: return queued ---
        if self.mode in (
            "async_success", "async_error", "async_timeout",
            "async_artifact_url", "async_artifact_path",
        ):
            import uuid
            job_id = f"fake_job_{uuid.uuid4().hex[:8]}"
            self.jobs[job_id] = {
                "width": width,
                "height": height,
                "seed": seed,
                "panel_id": panel_id,
                "status": "queued",
            }
            self.poll_count[job_id] = 0
            return aiohttp.web.json_response({
                "status": "queued",
                "job_id": job_id,
                "metadata": {"executor": "fake-remote"},
            })

        # --- missing image_base64 ---
        if self.mode == "missing_image":
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- invalid base64 ---
        if self.mode == "invalid_base64":
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "image_base64": "this-is-not-base64!!!",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- invalid image bytes (valid base64, not a valid image) ---
        if self.mode == "invalid_image":
            fake_bytes = base64.b64encode(b"not-an-image").decode("ascii")
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "image_base64": fake_bytes,
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- artifact_url: store image bytes and return URL ---
        if self.mode == "artifact_url":
            img_bytes = self._make_image_bytes(width, height, seed)
            self._store_artifact(f"{panel_id}.png", img_bytes)
            base = f"http://127.0.0.1:{self._server_port}"
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "artifact_url": f"{base}/artifacts/{panel_id}.png",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- artifact_url_404: return URL but don't store artifact ---
        if self.mode == "artifact_url_404":
            base = f"http://127.0.0.1:{self._server_port}"
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "artifact_url": f"{base}/artifacts/{panel_id}.png",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- artifact_path: save to tmp and return path ---
        if self.mode == "artifact_path":
            img_bytes = self._make_image_bytes(width, height, seed)
            tmp_dir = self._get_tmp_dir()
            file_path = os.path.join(tmp_dir, f"{panel_id}.png")
            with open(file_path, "wb") as f:
                f.write(img_bytes)
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "artifact_path": file_path,
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- artifact_path_missing: return path but don't create file ---
        if self.mode == "artifact_path_missing":
            return aiohttp.web.json_response({
                "status": "completed",
                "filename": f"{panel_id}.png",
                "artifact_path": "/nonexistent/path/fake.png",
                "seed": seed,
                "metadata": {"executor": "fake-remote"},
            })

        # --- success (default) ---
        b64 = self._make_image_b64(width, height, seed)
        return aiohttp.web.json_response({
            "status": "completed",
            "filename": f"{panel_id}.png",
            "image_base64": b64,
            "seed": seed,
            "metadata": {
                "executor": "fake-remote",
                "prompt_id": f"fake_{panel_id}",
            },
        })

    # ---- GET /v1/jobs/{job_id} ----

    async def handle_get_job(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        job_id = request.match_info["job_id"]
        self.job_requests.append({"job_id": job_id})

        job = self.jobs.get(job_id)
        if job is None:
            return aiohttp.web.json_response(
                {"status": "error", "error": f"job {job_id!r} not found"},
            )

        self.poll_count[job_id] = self.poll_count.get(job_id, 0) + 1
        poll_n = self.poll_count[job_id]

        if self.mode == "async_timeout":
            # Always running — never completes.
            return aiohttp.web.json_response({
                "status": "running",
                "job_id": job_id,
            })

        if self.mode == "async_error":
            # First poll returns running, second returns error.
            if poll_n < 2:
                return aiohttp.web.json_response({
                    "status": "running",
                    "job_id": job_id,
                })
            return aiohttp.web.json_response({
                "status": "error",
                "job_id": job_id,
                "error": "model failed",
            })

        if self.mode == "async_success":
            # First poll returns running, second returns completed.
            if poll_n < 2:
                return aiohttp.web.json_response({
                    "status": "running",
                    "job_id": job_id,
                })
            b64 = self._make_image_b64(
                job["width"], job["height"], job["seed"],
            )
            return aiohttp.web.json_response({
                "status": "completed",
                "job_id": job_id,
                "filename": f"{job['panel_id']}.png",
                "image_base64": b64,
                "seed": job["seed"],
                "metadata": {
                    "executor": "fake-remote",
                    "prompt_id": f"fake_{job_id}",
                },
            })

        if self.mode == "async_artifact_url":
            # First poll returns running, second returns completed with artifact_url.
            if poll_n < 2:
                return aiohttp.web.json_response({
                    "status": "running",
                    "job_id": job_id,
                })
            img_bytes = self._make_image_bytes(
                job["width"], job["height"], job["seed"],
            )
            self._store_artifact(f"{job['panel_id']}.png", img_bytes)
            base = f"http://127.0.0.1:{self._server_port}"
            return aiohttp.web.json_response({
                "status": "completed",
                "job_id": job_id,
                "filename": f"{job['panel_id']}.png",
                "artifact_url": f"{base}/artifacts/{job['panel_id']}.png",
                "seed": job["seed"],
                "metadata": {
                    "executor": "fake-remote",
                    "prompt_id": f"fake_{job_id}",
                },
            })

        if self.mode == "async_artifact_path":
            # First poll returns running, second returns completed with artifact_path.
            if poll_n < 2:
                return aiohttp.web.json_response({
                    "status": "running",
                    "job_id": job_id,
                })
            img_bytes = self._make_image_bytes(
                job["width"], job["height"], job["seed"],
            )
            tmp_dir = self._get_tmp_dir()
            file_path = os.path.join(tmp_dir, f"{job['panel_id']}.png")
            with open(file_path, "wb") as f:
                f.write(img_bytes)
            return aiohttp.web.json_response({
                "status": "completed",
                "job_id": job_id,
                "filename": f"{job['panel_id']}.png",
                "artifact_path": file_path,
                "seed": job["seed"],
                "metadata": {
                    "executor": "fake-remote",
                    "prompt_id": f"fake_{job_id}",
                },
            })

        return aiohttp.web.json_response({
            "status": "error",
            "job_id": job_id,
            "error": f"unknown mode: {self.mode}",
        })

    # ---- GET /artifacts/{filename} ----

    async def handle_get_artifact(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        filename = request.match_info["filename"]
        image_bytes = self.artifacts.get(filename)
        if image_bytes is None:
            return aiohttp.web.Response(
                status=404,
                text=f"artifact not found: {filename}",
                content_type="text/plain",
            )
        return aiohttp.web.Response(
            body=image_bytes,
            content_type="image/png",
        )

    def app(self) -> aiohttp.web.Application:
        app = aiohttp.web.Application()
        app.router.add_post("/v1/generate-panel", self.handle_generate)
        app.router.add_get("/v1/jobs/{job_id}", self.handle_get_job)
        app.router.add_get("/artifacts/{filename}", self.handle_get_artifact)
        return app

    async def start(self, runner: Any) -> None:
        """Store the server port after runner starts."""
        self._server_port = 0  # will be set by test helper


__all__ = [
    "RemoteExecutorError",
    "RemoteExecutorHTTPError",
    "RemoteExecutorTimeoutError",
    "RemoteExecutorResponseError",
    "RemoteExecutorImageError",
    "RemoteExecutorPollingTimeoutError",
    "RemoteExecutorJobError",
    "RemoteWorkerSettings",
    "RemoteGenerateRequest",
    "RemoteGenerateResponse",
    "RemoteHTTPExecutor",
    "FakeRemoteWorker",
]
