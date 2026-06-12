"""Real S3/R2 artifact store smoke test — opt-in E2E.

This test uploads to a real S3-compatible service.  It is skipped by
default and only runs when all required environment variables are set:

    MANGA_AUTOPILOT_REAL_S3_E2E=1
    MANGA_AUTOPILOT_S3_BUCKET=...
    MANGA_AUTOPILOT_S3_ACCESS_KEY_ID=...
    MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY=...

Usage::

    MANGA_AUTOPILOT_REAL_S3_E2E=1 \\
    MANGA_AUTOPILOT_S3_ENDPOINT_URL=https://<accountid>.r2.cloudflarestorage.com \\
    MANGA_AUTOPILOT_S3_BUCKET=manga-autopilot \\
    MANGA_AUTOPILOT_S3_REGION=auto \\
    MANGA_AUTOPILOT_S3_ACCESS_KEY_ID=... \\
    MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY=... \\
    pytest tests/backend/test_real_s3_artifact_store_e2e.py -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure src is importable.
_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


# ------------------------------------------------------------------ skip guard

_REAL_S3_E2E = os.environ.get("MANGA_AUTOPILOT_REAL_S3_E2E", "0") == "1"
_S3_BUCKET = os.environ.get("MANGA_AUTOPILOT_S3_BUCKET", "")
_S3_ACCESS_KEY = os.environ.get("MANGA_AUTOPILOT_S3_ACCESS_KEY_ID", "")
_S3_SECRET_KEY = os.environ.get("MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY", "")

_SKIP_REASON = (
    "Real S3/R2 E2E skipped.  Set "
    "MANGA_AUTOPILOT_REAL_S3_E2E=1, "
    "MANGA_AUTOPILOT_S3_BUCKET, "
    "MANGA_AUTOPILOT_S3_ACCESS_KEY_ID, and "
    "MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY to run."
)


# --------------------------------------------------------------- smoke test


@pytest.mark.skipif(not _REAL_S3_E2E, reason=_SKIP_REASON)
@pytest.mark.skipif(not _S3_BUCKET, reason=_SKIP_REASON)
@pytest.mark.skipif(not _S3_ACCESS_KEY, reason=_SKIP_REASON)
@pytest.mark.skipif(not _S3_SECRET_KEY, reason=_SKIP_REASON)
class TestRealS3ArtifactStoreE2E:
    """Real S3/R2 artifact store smoke tests."""

    def test_upload_and_retrieve(self):
        from manga_autopilot.services.artifact_store import S3CompatibleArtifactStore

        store = S3CompatibleArtifactStore(
            endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
            bucket=_S3_BUCKET,
            region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
            access_key_id=_S3_ACCESS_KEY,
            secret_access_key=_S3_SECRET_KEY,
            public_base_url=os.environ.get("MANGA_AUTOPILOT_S3_PUBLIC_BASE_URL"),
        )

        # Minimal 1x1 PNG.
        import base64

        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
        )

        result = store.upload_bytes(
            key="test/e2e-smoke.png",
            data=png_data,
            content_type="image/png",
            metadata={"test": "e2e-smoke"},
        )

        assert result.artifact_key == "test/e2e-smoke.png"
        assert result.size_bytes == len(png_data)

        if result.artifact_url:
            assert result.artifact_url.startswith("http")

    def test_public_url_generated_when_configured(self):
        from manga_autopilot.services.artifact_store import S3CompatibleArtifactStore

        public_base = os.environ.get("MANGA_AUTOPILOT_S3_PUBLIC_BASE_URL")
        if not public_base:
            pytest.skip("MANGA_AUTOPILOT_S3_PUBLIC_BASE_URL not set")

        store = S3CompatibleArtifactStore(
            endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
            bucket=_S3_BUCKET,
            region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
            access_key_id=_S3_ACCESS_KEY,
            secret_access_key=_S3_SECRET_KEY,
            public_base_url=public_base,
        )

        result = store.upload_bytes(
            key="test/e2e-url.png",
            data=b"\x89PNG\r\n\x1a\n",
            content_type="image/png",
        )

        assert result.artifact_url is not None
        assert result.artifact_url.startswith(public_base)
