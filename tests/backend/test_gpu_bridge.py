"""Tests for the external GPU bridge (spec section 24)."""

from __future__ import annotations

import base64
import binascii
import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from manga_autopilot.services.gpu_bridge import (
    ExternalGPUClient,
    GPUBridge,
    GPUFallbackPolicy,
    WorkerError,
    WorkerGenerateRequest,
    WorkerGenerateResponse,
    WorkerHandle,
    WorkerImageResult,
    WorkerSettings,
    cleanup_tempdir,
    decode_base64_image,
    serialize_workflow,
    write_assets_to_tempdir,
)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# ----------------------------------------------------------- request/response
def test_worker_request_generates_job_id() -> None:
    req = WorkerGenerateRequest(workflow={"a": 1})
    assert req.job_id.startswith("job_")
    assert req.workflow == {"a": 1}


def test_worker_request_validation() -> None:
    from pydantic import ValidationError as VE
    with pytest.raises(VE):
        WorkerSettings(timeout_sec=1)  # below min
    with pytest.raises(VE):
        WorkerGenerateResponse.model_validate({"success": True, "job_id": 123})  # job_id must be str


# ----------------------------------------------------------- serialization
def test_serialize_workflow_packs_assets(tmp_path: Path) -> None:
    ref = tmp_path / "ref.png"
    ref.write_bytes(_png())
    out = serialize_workflow(
        workflow={"nodes": {}},
        assets=[{"name": "ref.png", "path": str(ref)}],
    )
    assert out["job_id"].startswith("job_")
    assert out["workflow"] == {"nodes": {}}
    assert out["assets"][0]["name"] == "ref.png"
    raw = base64.b64decode(out["assets"][0]["content_base64"], validate=True)
    assert raw == _png()


def test_serialize_workflow_passes_through_base64() -> None:
    b64 = base64.b64encode(_png()).decode("ascii")
    out = serialize_workflow(
        workflow={},
        assets=[{"name": "x.png", "content_base64": b64}],
    )
    assert out["assets"][0]["content_base64"] == b64


def test_serialize_workflow_settings() -> None:
    out = serialize_workflow({}, settings={"timeout_sec": 60})
    assert out["settings"]["timeout_sec"] == 60


# ----------------------------------------------------------- decode
def test_decode_base64_image() -> None:
    img = decode_base64_image(base64.b64encode(_png()).decode("ascii"))
    assert img.size == (16, 16)


def test_decode_base64_image_rejects_bad() -> None:
    with pytest.raises((binascii.Error, ValueError)):
        decode_base64_image("not-base64!!!")


# ----------------------------------------------------------- temp
def test_write_assets_to_tempdir(tmp_path: Path) -> None:
    payload = {
        "assets": [
            {"name": "a.png", "content_base64": base64.b64encode(_png()).decode("ascii")},
            {"name": "b.png", "content_base64": base64.b64encode(b"hello").decode("ascii")},
        ]
    }
    written = write_assets_to_tempdir(payload, tmp_path)
    assert len(written) == 2
    assert all(w.path.exists() for w in written)


def test_write_assets_to_tempdir_skips_incomplete() -> None:
    written = write_assets_to_tempdir(
        {"assets": [{"name": "x.png"}, {"name": "", "content_base64": "abc"}]},
        Path("/tmp"),
    )
    assert written == []


def test_cleanup_tempdir(tmp_path: Path) -> None:
    assert cleanup_tempdir(tmp_path)
    assert not tmp_path.exists()


def test_cleanup_tempdir_missing() -> None:
    assert cleanup_tempdir(Path("/nonexistent/path"))


# ----------------------------------------------------------- client
async def test_external_gpu_client_success() -> None:
    captured: dict[str, Any] = {}

    def fake_session():
        class _Resp:
            status = 200

            async def json(self):
                captured["called"] = True
                return {
                    "success": True,
                    "job_id": "job_1",
                    "images": [
                        {
                            "filename": "x.png",
                            "content_base64": base64.b64encode(_png()).decode("ascii"),
                            "width": 16,
                            "height": 16,
                        }
                    ],
                    "logs": [],
                    "deleted_temp": True,
                }

            async def text(self):
                return "ok"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def post(self, url, **kwargs):
                captured["url"] = url
                return _Resp()

        return _Session()

    client = ExternalGPUClient(
        worker=WorkerHandle(name="w1", endpoint="https://example.test"),
        session_factory=fake_session,
    )
    request = WorkerGenerateRequest(workflow={})
    response = await client.generate(request)
    assert response.success
    assert captured["url"] == "https://example.test/generate"
    assert len(response.images) == 1


async def test_external_gpu_client_records_failure() -> None:
    def fake_session():
        class _Resp:
            status = 503

            async def text(self):
                return "service down"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def post(self, url, **kwargs):
                return _Resp()

        return _Session()

    client = ExternalGPUClient(
        worker=WorkerHandle(name="w1", endpoint="https://example.test"),
        session_factory=fake_session,
    )
    with pytest.raises(WorkerError):
        await client.generate(WorkerGenerateRequest(workflow={}))


# ----------------------------------------------------------- fallback
def test_should_fallback_on_timeout() -> None:
    policy = GPUFallbackPolicy()
    assert policy.should_fallback("timeout")
    assert policy.should_fallback("gpu_unavailable")
    assert not policy.should_fallback("generation_error")


def test_fallback_disabled() -> None:
    policy = GPUFallbackPolicy(enabled=False)
    assert not policy.should_fallback("timeout")


async def test_fallback_runs_local_executor() -> None:
    async def local(req):
        return WorkerGenerateResponse(
            success=True, job_id=req.job_id, images=[
                WorkerImageResult(filename="x.png", content_base64="x", width=1, height=1)
            ]
        )

    policy = GPUFallbackPolicy(local_executor=local)
    res = await policy.fallback(WorkerGenerateRequest(workflow={}), "timeout")
    assert res.success


async def test_fallback_no_executor() -> None:
    policy = GPUFallbackPolicy(enabled=True, local_executor=None)
    res = await policy.fallback(WorkerGenerateRequest(workflow={}), "timeout")
    assert not res.success
    assert "unavailable" in (res.error or "")


# ----------------------------------------------------------- bridge
async def test_bridge_success() -> None:
    def fake_session():
        class _Resp:
            status = 200

            async def json(self):
                return {
                    "success": True,
                    "job_id": "job_1",
                    "images": [
                        {
                            "filename": "x.png",
                            "content_base64": base64.b64encode(_png()).decode("ascii"),
                            "width": 16,
                            "height": 16,
                        }
                    ],
                    "logs": [],
                    "deleted_temp": False,
                }

            async def text(self):
                return ""

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class _Session:
            def post(self, url, **kwargs):
                return _Resp()

        return _Session()

    async def local(req):
        return WorkerGenerateResponse(success=False, job_id=req.job_id, error="should not run")

    client = ExternalGPUClient(
        worker=WorkerHandle(name="w1", endpoint="https://example.test"),
        session_factory=fake_session,
    )
    bridge = GPUBridge(
        client=client,
        fallback=GPUFallbackPolicy(local_executor=local),
    )
    req = WorkerGenerateRequest(workflow={"a": 1})
    response, failure = await bridge.submit(req)
    assert failure is None
    assert response.success


async def test_bridge_falls_back_on_timeout(tmp_path: Path) -> None:
    class _TimeoutSession:
        def post(self, url, **kwargs):
            raise __import__("asyncio").TimeoutError()

    def fake_session():
        return _TimeoutSession()

    async def local(req):
        return WorkerGenerateResponse(success=True, job_id=req.job_id)

    client = ExternalGPUClient(
        worker=WorkerHandle(name="w1", endpoint="https://example.test"),
        session_factory=fake_session,
    )
    bridge = GPUBridge(
        client=client,
        fallback=GPUFallbackPolicy(local_executor=local),
        temp_dir=tmp_path,
    )
    response, failure = await bridge.submit(WorkerGenerateRequest(workflow={}))
    assert failure == "timeout"
    assert response.success
