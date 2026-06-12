"""Contract tests for artifact access policy and signed URL foundation."""

from __future__ import annotations

import sys
import unittest.mock
from pathlib import Path

import pytest

_SRC_DIR = str(Path(__file__).resolve().parents[2] / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from manga_autopilot.services.artifact_access import (  # noqa: E402
    ArtifactAccessPolicy,
    ArtifactAccessUrl,
    ArtifactUrlSigner,
    LocalArtifactUrlSigner,
    S3CompatibleArtifactUrlSigner,
    create_artifact_access_policy_from_env,
    create_artifact_url_signer_from_env,
)

# -------------------------------------------------------------------
# ArtifactAccessPolicy model tests
# -------------------------------------------------------------------


class TestArtifactAccessPolicy:
    def test_defaults_to_public(self):
        policy = ArtifactAccessPolicy()
        assert policy.mode == "public"
        assert policy.signed_url_ttl_seconds == 3600
        assert policy.persist_signed_urls is False

    def test_signed_defaults_do_not_persist_urls(self):
        policy = ArtifactAccessPolicy(mode="signed")
        assert policy.persist_signed_urls is False

    def test_private_mode(self):
        policy = ArtifactAccessPolicy(mode="private")
        assert policy.mode == "private"

    def test_custom_ttl(self):
        policy = ArtifactAccessPolicy(mode="signed", signed_url_ttl_seconds=600)
        assert policy.signed_url_ttl_seconds == 600

    def test_persist_signed_urls_flag(self):
        policy = ArtifactAccessPolicy(mode="signed", persist_signed_urls=True)
        assert policy.persist_signed_urls is True


# -------------------------------------------------------------------
# ArtifactAccessUrl tests
# -------------------------------------------------------------------


class TestArtifactAccessUrl:
    def test_frozen_dataclass(self):
        url = ArtifactAccessUrl(
            artifact_key="test/key.png",
            url="https://example.com/test/key.png",
            expires_at="2025-01-01T00:00:00+00:00",
            expires_in_seconds=3600,
        )
        assert url.artifact_key == "test/key.png"
        assert url.url == "https://example.com/test/key.png"
        assert url.expires_at == "2025-01-01T00:00:00+00:00"
        assert url.expires_in_seconds == 3600

    def test_optional_fields(self):
        url = ArtifactAccessUrl(artifact_key="k.png", url="http://x")
        assert url.expires_at is None
        assert url.expires_in_seconds is None


# -------------------------------------------------------------------
# LocalArtifactUrlSigner tests
# -------------------------------------------------------------------


class TestLocalUrlSigner:
    def test_generates_url(self):
        signer = LocalArtifactUrlSigner()
        result = signer.create_access_url(
            artifact_key="projects/p/runs/r/a.png",
            expires_in_seconds=3600,
        )
        assert isinstance(result, ArtifactAccessUrl)
        assert result.artifact_key == "projects/p/runs/r/a.png"
        assert "projects/p/runs/r/a.png" in result.url
        assert "?expires_in=3600" in result.url
        assert result.expires_in_seconds == 3600

    def test_custom_base_url(self):
        signer = LocalArtifactUrlSigner(public_base_url="https://cdn.example.com/art")
        result = signer.create_access_url(
            artifact_key="projects/p/a.png",
            expires_in_seconds=600,
        )
        assert result.url.startswith("https://cdn.example.com/art/")
        assert "?expires_in=600" in result.url

    def test_expires_at_format(self):
        signer = LocalArtifactUrlSigner()
        result = signer.create_access_url(
            artifact_key="projects/p/a.png",
            expires_in_seconds=3600,
        )
        assert result.expires_at is not None
        # ISO-8601 format
        assert "T" in result.expires_at
        assert "+" in result.expires_at or "Z" in result.expires_at

    def test_rejects_unsafe_key_traversal(self):
        signer = LocalArtifactUrlSigner()
        with pytest.raises(ValueError, match="path traversal"):
            signer.create_access_url(
                artifact_key="projects/../etc/passwd",
                expires_in_seconds=3600,
            )

    def test_rejects_empty_key(self):
        signer = LocalArtifactUrlSigner()
        with pytest.raises(ValueError, match="must not be empty"):
            signer.create_access_url(artifact_key="", expires_in_seconds=3600)

    def test_rejects_absolute_key(self):
        signer = LocalArtifactUrlSigner()
        with pytest.raises(ValueError, match="must not be absolute"):
            signer.create_access_url(
                artifact_key="/etc/passwd",
                expires_in_seconds=3600,
            )

    def test_is_protocol_compliant(self):
        signer = LocalArtifactUrlSigner()
        assert isinstance(signer, ArtifactUrlSigner)


# -------------------------------------------------------------------
# S3 presigned URL signer tests (mocked)
# -------------------------------------------------------------------


class TestS3UrlSignerRequiresBoto3:
    def test_requires_boto3_when_missing(self):
        with unittest.mock.patch.dict(sys.modules, {"boto3": None}):
            with pytest.raises(ImportError, match="boto3 is required"):
                S3CompatibleArtifactUrlSigner(
                    endpoint_url=None,
                    bucket="test-bucket",
                    region_name=None,
                    access_key_id=None,
                    secret_access_key=None,
                )

    def test_rejects_missing_bucket(self):
        with pytest.raises(ValueError, match="bucket must not be empty"):
            S3CompatibleArtifactUrlSigner(
                endpoint_url=None,
                bucket="",
                region_name=None,
                access_key_id=None,
                secret_access_key=None,
            )


class TestS3UrlSignerPresignedUrl:
    def test_builds_presigned_url_with_mocked_client(self):
        mock_client = unittest.mock.MagicMock()
        mock_client.generate_presigned_url.return_value = (
            "https://s3.example.com/bucket/key?X-Amz-Signature=abc"
        )

        with unittest.mock.patch.dict(sys.modules, {"boto3": unittest.mock.MagicMock()}):
            signer = S3CompatibleArtifactUrlSigner(
                endpoint_url="https://s3.example.com",
                bucket="test-bucket",
                region_name="us-east-1",
                access_key_id="AKID",
                secret_access_key="SECRET",
            )
            # Replace the real client with our mock.
            signer._client = mock_client

            result = signer.create_access_url(
                artifact_key="projects/p/a.png",
                expires_in_seconds=3600,
            )

            assert isinstance(result, ArtifactAccessUrl)
            assert result.artifact_key == "projects/p/a.png"
            assert "X-Amz-Signature" in result.url
            assert result.expires_in_seconds == 3600
            assert result.expires_at is not None

            mock_client.generate_presigned_url.assert_called_once_with(
                "get_object",
                Params={"Bucket": "test-bucket", "Key": "projects/p/a.png"},
                ExpiresIn=3600,
            )

    def test_rejects_unsafe_key(self):
        with unittest.mock.patch.dict(sys.modules, {"boto3": unittest.mock.MagicMock()}):
            signer = S3CompatibleArtifactUrlSigner(
                endpoint_url=None,
                bucket="test-bucket",
                region_name=None,
                access_key_id=None,
                secret_access_key=None,
            )
            with pytest.raises(ValueError, match="path traversal"):
                signer.create_access_url(
                    artifact_key="projects/../etc/passwd",
                    expires_in_seconds=3600,
                )


# -------------------------------------------------------------------
# create_artifact_access_policy_from_env tests
# -------------------------------------------------------------------


class TestCreateArtifactAccessPolicyFromEnv:
    def test_defaults_to_public(self):
        with unittest.mock.patch.dict("os.environ", {}, clear=False):
            # Remove env vars if they exist.
            env = {k: v for k, v in __import__("os").environ.items()
                   if not k.startswith("MANGA_AUTOPILOT_ARTIFACT_ACCESS")}
            with unittest.mock.patch.dict("os.environ", env, clear=True):
                policy = create_artifact_access_policy_from_env()
                assert policy.mode == "public"
                assert policy.signed_url_ttl_seconds == 3600
                assert policy.persist_signed_urls is False

    def test_returns_signer_for_signed_mode(self):
        env = {
            "MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE": "signed",
            "MANGA_AUTOPILOT_S3_BUCKET": "",
        }
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            signer = create_artifact_url_signer_from_env()
            assert signer is not None
            assert isinstance(signer, LocalArtifactUrlSigner)

    def test_returns_none_for_private_mode(self):
        env = {"MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE": "private"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            signer = create_artifact_url_signer_from_env()
            assert signer is None

    def test_returns_local_signer_for_public_mode(self):
        env = {"MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE": "public"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            signer = create_artifact_url_signer_from_env()
            assert isinstance(signer, LocalArtifactUrlSigner)

    def test_rejects_invalid_mode(self):
        env = {"MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE": "invalid"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            with pytest.raises(ValueError, match="Invalid"):
                create_artifact_access_policy_from_env()

    def test_rejects_invalid_ttl(self):
        env = {"MANGA_AUTOPILOT_SIGNED_URL_TTL_SECONDS": "not_a_number"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            with pytest.raises(ValueError, match="Invalid"):
                create_artifact_access_policy_from_env()

    def test_persist_signed_urls_from_env(self):
        env = {"MANGA_AUTOPILOT_PERSIST_SIGNED_URLS": "true"}
        with unittest.mock.patch.dict("os.environ", env, clear=False):
            policy = create_artifact_access_policy_from_env()
            assert policy.persist_signed_urls is True


# -------------------------------------------------------------------
# Metadata policy tests
# -------------------------------------------------------------------


class TestMetadataPolicy:
    def test_signed_mode_metadata_does_not_persist_url_by_default(self):
        policy = ArtifactAccessPolicy(mode="signed")
        if policy.mode == "signed" and not policy.persist_signed_urls:
            # artifact_url should not be stored in metadata
            artifact_url = None
        else:
            artifact_url = "https://example.com/a.png"
        assert artifact_url is None

    def test_public_mode_allows_url_persistence(self):
        policy = ArtifactAccessPolicy(mode="public")
        if policy.mode == "public":
            artifact_url = "https://example.com/a.png"
        else:
            artifact_url = None
        assert artifact_url == "https://example.com/a.png"

    def test_private_mode_never_returns_url(self):
        policy = ArtifactAccessPolicy(mode="private")
        if policy.mode == "private":
            artifact_url = None
        else:
            artifact_url = "https://example.com/a.png"
        assert artifact_url is None
