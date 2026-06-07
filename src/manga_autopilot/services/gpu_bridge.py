"""External GPU bridge (spec section 24).

Modules:

- :class:`WorkerGenerateRequest` / :class:`WorkerGenerateResponse` - JSON
  schemas for the worker ``/generate`` endpoint.
- :func:`serialize_workflow` - package a workflow + assets for transfer.
- :class:`ExternalGPUClient` - async HTTP client that POSTs to a remote
  worker with timeout / cleanup hooks.
- :class:`WorkerHandle` - simple dataclass for a remote worker.
- :func:`decode_base64_image` - decode a base64 image into a PIL Image.
- :class:`GPUFallbackPolicy` - local ComfyUI fallback when the worker fails.
- :class:`WorkerRef` - base64 asset ref helper.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import aiohttp
from PIL import Image
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


ReturnType = Literal["base64", "url"]
CleanupPolicy = Literal["delete_temp_after_return", "keep_temp"]
FailureMode = Literal["timeout", "gpu_unavailable", "generation_error", "result_missing", "cleanup_failure"]


# ----------------------------------------------------------- request/response
class AssetPayload(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    content_base64: str
    kind: Literal["image", "workflow", "other"] = "image"


class WorkerSettings(BaseModel):
    return_type: ReturnType = "base64"
    delete_temp_after_return: bool = True
    timeout_sec: int = Field(default=900, ge=10, le=24 * 3600)


class WorkerGenerateRequest(BaseModel):
    job_id: str = Field(default_factory=lambda: f"job_{uuid.uuid4().hex[:12]}")
    workflow: dict[str, Any]
    assets: list[AssetPayload] = Field(default_factory=list)
    settings: WorkerSettings = Field(default_factory=WorkerSettings)


class WorkerImageResult(BaseModel):
    filename: str
    content_base64: str
    width: int = 0
    height: int = 0
    mime: str = "image/png"


class WorkerGenerateResponse(BaseModel):
    success: bool
    job_id: str
    images: list[WorkerImageResult] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    deleted_temp: bool = False
    error: str | None = None


# ----------------------------------------------------------- serialization
def serialize_workflow(
    workflow: Mapping[str, Any],
    *,
    assets: Sequence[Mapping[str, Any]] | None = None,
    job_id: str | None = None,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Package a workflow + assets into a worker-friendly dict (spec 24.3)."""

    asset_payloads: list[dict[str, Any]] = []
    for asset in assets or []:
        path = asset.get("path")
        if path is None:
            content = asset.get("content_base64")
        else:
            data = Path(path).read_bytes()
            content = base64.b64encode(data).decode("ascii")
        asset_payloads.append(
            {
                "name": asset.get("name", Path(str(path)).name if path else f"asset_{len(asset_payloads)}"),
                "content_base64": content,
                "kind": asset.get("kind", "image"),
            }
        )
    settings_obj = WorkerSettings(**(settings or {}))
    return {
        "job_id": job_id or f"job_{uuid.uuid4().hex[:12]}",
        "workflow": dict(workflow),
        "assets": asset_payloads,
        "settings": settings_obj.model_dump(),
    }


# ----------------------------------------------------------- decoding
def decode_base64_image(b64: str) -> Image.Image:
    """Decode a base64 string into a PIL Image."""

    raw = base64.b64decode(b64, validate=True)
    return Image.open(io.BytesIO(raw)).copy()


# ----------------------------------------------------------- local temp
@dataclass
class TempAsset:
    """Local file written to disk from a base64 asset for the worker."""

    name: str
    path: Path
    size: int


def write_assets_to_tempdir(
    request_payload: Mapping[str, Any],
    temp_dir: str | Path,
) -> list[TempAsset]:
    """Write all assets in the request to ``temp_dir`` and return file paths."""

    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    written: list[TempAsset] = []
    for asset in request_payload.get("assets", []):
        name = asset.get("name")
        b64 = asset.get("content_base64")
        if not name or not b64:
            continue
        path = temp_dir / name
        data = base64.b64decode(b64, validate=True)
        path.write_bytes(data)
        written.append(TempAsset(name=name, path=path, size=len(data)))
    return written


def cleanup_tempdir(temp_dir: str | Path) -> bool:
    """Delete the temp dir; returns True on success."""

    temp_dir = Path(temp_dir)
    if not temp_dir.exists():
        return True
    try:
        shutil.rmtree(temp_dir)
        return True
    except OSError as exc:
        log.warning("cleanup failed for %s: %s", temp_dir, exc)
        return False


# ----------------------------------------------------------- client
@dataclass
class WorkerHandle:
    name: str
    endpoint: str
    api_key: str | None = None
    enabled: bool = True


class WorkerError(Exception):
    pass


class _LocalExecutor(Protocol):
    async def __call__(self, request: WorkerGenerateRequest) -> WorkerGenerateResponse: ...


@dataclass
class ExternalGPUClient:
    """Async client for a remote GPU worker (spec 24.3)."""

    worker: WorkerHandle
    session_factory: Callable[[], aiohttp.ClientSession] | None = None
    failure_history: list[FailureMode] = field(default_factory=list)

    def _open(self) -> aiohttp.ClientSession:
        if self.session_factory is not None:
            return self.session_factory()
        return aiohttp.ClientSession()

    async def generate(self, request: WorkerGenerateRequest) -> WorkerGenerateResponse:
        url = self.worker.endpoint.rstrip("/") + "/generate"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.worker.api_key:
            headers["Authorization"] = f"Bearer {self.worker.api_key}"
        timeout = aiohttp.ClientTimeout(total=request.settings.timeout_sec)
        session = self._open()
        try:
            async with session.post(url, json=request.model_dump(), headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    raise WorkerError(f"worker returned {resp.status}: {await resp.text()}")
                data = await resp.json()
        finally:
            if self.session_factory is None:
                await session.close()
        return WorkerGenerateResponse.model_validate(data)

    def record_failure(self, mode: FailureMode) -> None:
        self.failure_history.append(mode)


# ----------------------------------------------------------- fallback
@dataclass
class GPUFallbackPolicy:
    """Local ComfyUI fallback when the external worker fails (spec 24.5)."""

    enabled: bool = True
    local_executor: _LocalExecutor | None = None
    failure_history: list[FailureMode] = field(default_factory=list)

    def should_fallback(self, failure: FailureMode) -> bool:
        if not self.enabled:
            return False
        if failure in ("timeout", "gpu_unavailable", "result_missing"):
            return True
        return False

    async def fallback(self, request: WorkerGenerateRequest, failure: FailureMode) -> WorkerGenerateResponse:
        self.failure_history.append(failure)
        if self.local_executor is None:
            return WorkerGenerateResponse(
                success=False,
                job_id=request.job_id,
                error=f"local executor unavailable (failure: {failure})",
            )
        return await self.local_executor(request)


# ----------------------------------------------------------- orchestrator
@dataclass
class GPUBridge:
    """Top-level bridge that wraps client + fallback + temp cleanup."""

    client: ExternalGPUClient
    fallback: GPUFallbackPolicy
    temp_dir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="gpu_bridge_")))

    async def submit(
        self,
        request: WorkerGenerateRequest,
    ) -> tuple[WorkerGenerateResponse, FailureMode | None]:
        # 1. Write assets to temp
        write_assets_to_tempdir(request.model_dump(), self.temp_dir)
        failure: FailureMode | None = None
        try:
            response = await self.client.generate(request)
            if not response.success:
                failure = "generation_error"
        except asyncio.TimeoutError:
            failure = "timeout"
            response = WorkerGenerateResponse(success=False, job_id=request.job_id, error="timeout")
        except aiohttp.ClientError as exc:
            failure = "gpu_unavailable"
            response = WorkerGenerateResponse(success=False, job_id=request.job_id, error=str(exc))
        except WorkerError as exc:
            failure = "generation_error"
            response = WorkerGenerateResponse(success=False, job_id=request.job_id, error=str(exc))

        if failure is not None:
            self.client.record_failure(failure)
            if self.fallback.should_fallback(failure):
                response = await self.fallback.fallback(request, failure)
                return response, failure
            return response, failure
        # 2. Cleanup temp
        if request.settings.delete_temp_after_return:
            ok = cleanup_tempdir(self.temp_dir)
            response.deleted_temp = ok
        return response, None


__all__ = [
    "AssetPayload",
    "CleanupPolicy",
    "ExternalGPUClient",
    "FailureMode",
    "GPUBridge",
    "GPUFallbackPolicy",
    "ReturnType",
    "TempAsset",
    "WorkerError",
    "WorkerGenerateRequest",
    "WorkerGenerateResponse",
    "WorkerHandle",
    "WorkerImageResult",
    "WorkerSettings",
    "cleanup_tempdir",
    "decode_base64_image",
    "serialize_workflow",
    "write_assets_to_tempdir",
]
