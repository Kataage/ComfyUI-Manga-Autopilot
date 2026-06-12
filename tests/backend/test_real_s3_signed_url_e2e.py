"""Opt-in S3/R2 signed URL E2E tests.

Requires real S3/R2 credentials and access mode set to 'signed'.

Skip conditions (all must be met):
    MANGA_AUTOPILOT_REAL_S3_E2E == 1
    MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE == signed
    MANGA_AUTOPILOT_S3_BUCKET is set
    MANGA_AUTOPILOT_S3_ACCESS_KEY_ID is set
    MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY is set
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

_SKIP_REASON = (
    "Real S3/R2 signed URL E2E skipped.  Set "
    "MANGA_AUTOPILOT_REAL_S3_E2E=1, MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE=signed, "
    "MANGA_AUTOPILOT_S3_BUCKET, MANGA_AUTOPILOT_S3_ACCESS_KEY_ID, and "
    "MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY to run."
)

_SHOULD_RUN = (
    os.environ.get("MANGA_AUTOPILOT_REAL_S3_E2E") == "1"
    and os.environ.get("MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE") == "signed"
    and bool(os.environ.get("MANGA_AUTOPILOT_S3_BUCKET"))
    and bool(os.environ.get("MANGA_AUTOPILOT_S3_ACCESS_KEY_ID"))
    and bool(os.environ.get("MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY"))
)

pytestmark = pytest.mark.skipif(not _SHOULD_RUN, reason=_SKIP_REASON)


@pytest.fixture()
def signer():
    from manga_autopilot.services.artifact_access import (
        S3CompatibleArtifactUrlSigner,
    )

    return S3CompatibleArtifactUrlSigner(
        endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
        bucket=os.environ["MANGA_AUTOPILOT_S3_BUCKET"],
        region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
        access_key_id=os.environ["MANGA_AUTOPILOT_S3_ACCESS_KEY_ID"],
        secret_access_key=os.environ["MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY"],
    )


@pytest.fixture()
def store():
    from manga_autopilot.services.artifact_store import S3CompatibleArtifactStore

    return S3CompatibleArtifactStore(
        endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
        bucket=os.environ["MANGA_AUTOPILOT_S3_BUCKET"],
        region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
        access_key_id=os.environ["MANGA_AUTOPILOT_S3_ACCESS_KEY_ID"],
        secret_access_key=os.environ["MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY"],
    )


class TestRealS3SignedUrlE2E:
    def test_upload_and_sign(self, store, signer):
        key = "e2e/signed-url-test/test_image.png"
        # Small 1x1 red PNG.
        data = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
            b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

        result = store.upload_bytes(key=key, data=data, content_type="image/png")
        assert result.artifact_key == key

        access_url = signer.create_access_url(
            artifact_key=key,
            expires_in_seconds=3600,
        )

        assert access_url.artifact_key == key
        assert isinstance(access_url.url, str)
        assert len(access_url.url) > 0
        assert access_url.expires_in_seconds == 3600
        assert access_url.expires_at is not None

    def test_signer_rejects_unsafe_key(self, signer):
        with pytest.raises(ValueError, match="path traversal"):
            signer.create_access_url(
                artifact_key="projects/../etc/passwd",
                expires_in_seconds=3600,
            )
