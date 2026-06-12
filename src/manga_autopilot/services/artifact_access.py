"""Artifact access policy and signed URL foundation for Manga Autopilot.

Defines how public URLs, private keys, and signed URLs are represented
without requiring user authentication or production CDN setup.

- ``ArtifactAccessPolicy``: controls URL mode and TTL
- ``ArtifactAccessUrl``: signed URL result container
- ``ArtifactUrlSigner``: protocol for generating signed URLs
- ``LocalArtifactUrlSigner``: CI/local development signer
- ``S3CompatibleArtifactUrlSigner``: boto3 presigned URL signer
- ``create_artifact_access_policy_from_env()``: env factory
- ``create_artifact_url_signer_from_env()``: env factory
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol, runtime_checkable

from manga_autopilot.services.artifact_store import _validate_artifact_key

# ------------------------------------------------------- access policy


@dataclass(frozen=True)
class ArtifactAccessPolicy:
    """Policy controlling artifact URL accessibility.

    Parameters
    ----------
    mode:
        ``"public"`` – artifact_url is stored in metadata.
        ``"private"`` – only artifact_key is stored; no URL returned.
        ``"signed"`` – artifact_key is stored; signed URL issued on demand.
    signed_url_ttl_seconds:
        Time-to-live for signed URLs.  Only used in ``"signed"`` mode.
    persist_signed_urls:
        If ``True``, signed URLs are stored in metadata (not recommended).
    """

    mode: Literal["public", "private", "signed"] = "public"
    signed_url_ttl_seconds: int = 3600
    persist_signed_urls: bool = False


# ---------------------------------------------------- access URL result


@dataclass(frozen=True)
class ArtifactAccessUrl:
    """Result of creating a signed/access URL for an artifact.

    Parameters
    ----------
    artifact_key:
        The canonical artifact key.
    url:
        The signed or public URL.
    expires_at:
        ISO-8601 UTC timestamp when the URL expires (``None`` for public).
    expires_in_seconds:
        TTL used to generate this URL (convenience).
    """

    artifact_key: str
    url: str
    expires_at: str | None = None
    expires_in_seconds: int | None = None


# --------------------------------------------------- signer protocol


@runtime_checkable
class ArtifactUrlSigner(Protocol):
    """Protocol for generating signed or public access URLs."""

    def create_access_url(
        self,
        *,
        artifact_key: str,
        expires_in_seconds: int,
    ) -> ArtifactAccessUrl:
        """Create an access URL for the given artifact key.

        Parameters
        ----------
        artifact_key:
            The artifact key (must pass safety validation).
        expires_in_seconds:
            TTL for signed URLs (ignored in public mode).

        Returns
        -------
        ArtifactAccessUrl
        """
        ...


# ------------------------------------------------- local URL signer


class LocalArtifactUrlSigner:
    """Signer that generates local/public URLs for CI and development.

    Produces URLs like ``http://localhost/artifacts/<key>?expires_in=<ttl>``.

    Parameters
    ----------
    public_base_url:
        Base URL for artifact access.
    """

    def __init__(self, public_base_url: str = "http://localhost/artifacts") -> None:
        self._public_base_url = public_base_url.rstrip("/")

    def create_access_url(
        self,
        *,
        artifact_key: str,
        expires_in_seconds: int,
    ) -> ArtifactAccessUrl:
        """Create a local/public URL for the artifact key.

        Raises ``ValueError`` if the artifact_key is unsafe.
        """
        _validate_artifact_key(artifact_key)

        url = f"{self._public_base_url}/{artifact_key}?expires_in={expires_in_seconds}"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
        ).isoformat()

        return ArtifactAccessUrl(
            artifact_key=artifact_key,
            url=url,
            expires_at=expires_at,
            expires_in_seconds=expires_in_seconds,
        )


# ----------------------------------------------- S3 presigned URL signer


class S3CompatibleArtifactUrlSigner:
    """Signer that generates S3 presigned URLs.

    Requires ``boto3`` (``pip install -e ".[s3]"``).

    Parameters
    ----------
    endpoint_url:
        S3-compatible endpoint URL (e.g. R2 endpoint).
    bucket:
        S3 bucket name.
    region_name:
        AWS region name (use ``"auto"`` for R2).
    access_key_id:
        AWS access key ID.
    secret_access_key:
        AWS secret access key.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        region_name: str | None,
        access_key_id: str | None,
        secret_access_key: str | None,
    ) -> None:
        if not bucket:
            raise ValueError("bucket must not be empty")

        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for S3 presigned URLs. "
                "Install with: pip install -e '.[s3]'"
            ) from exc

        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def create_access_url(
        self,
        *,
        artifact_key: str,
        expires_in_seconds: int,
    ) -> ArtifactAccessUrl:
        """Generate a presigned GET URL for the artifact key.

        Raises ``ValueError`` if the artifact_key is unsafe.
        """
        _validate_artifact_key(artifact_key)

        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": artifact_key},
            ExpiresIn=expires_in_seconds,
        )

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
        ).isoformat()

        return ArtifactAccessUrl(
            artifact_key=artifact_key,
            url=url,
            expires_at=expires_at,
            expires_in_seconds=expires_in_seconds,
        )


# ------------------------------------------------ factories from env


def create_artifact_access_policy_from_env() -> ArtifactAccessPolicy:
    """Create an artifact access policy from environment variables.

    Environment variables
    ---------------------
    - ``MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE``: ``"public"``, ``"private"``,
      or ``"signed"``.  Default ``"public"``.
    - ``MANGA_AUTOPILOT_SIGNED_URL_TTL_SECONDS``: TTL in seconds.
      Default ``3600``.
    - ``MANGA_AUTOPILOT_PERSIST_SIGNED_URLS``: ``"true"`` to persist signed
      URLs in metadata.  Default ``"false"``.
    """
    mode_str = os.environ.get("MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE", "public")
    if mode_str not in ("public", "private", "signed"):
        raise ValueError(
            f"Invalid MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE: {mode_str!r} "
            "(expected 'public', 'private', or 'signed')"
        )

    ttl_str = os.environ.get("MANGA_AUTOPILOT_SIGNED_URL_TTL_SECONDS", "3600")
    try:
        ttl = int(ttl_str)
    except ValueError as exc:
        raise ValueError(
            f"Invalid MANGA_AUTOPILOT_SIGNED_URL_TTL_SECONDS: {ttl_str!r}"
        ) from exc

    persist_str = os.environ.get("MANGA_AUTOPILOT_PERSIST_SIGNED_URLS", "false")
    persist = persist_str.lower() in ("true", "1", "yes")

    return ArtifactAccessPolicy(
        mode=mode_str,  # type: ignore[arg-type]
        signed_url_ttl_seconds=ttl,
        persist_signed_urls=persist,
    )


def create_artifact_url_signer_from_env() -> ArtifactUrlSigner | None:
    """Create an artifact URL signer from environment variables.

    Returns ``None`` if mode is ``"public"`` (no signer needed).

    Environment variables
    ---------------------
    - ``MANGA_AUTOPILOT_ARTIFACT_ACCESS_MODE``: ``"public"``, ``"private"``,
      or ``"signed"``.  Default ``"public"``.
    - S3/R2 credentials use the same env vars as the artifact store:
      ``MANGA_AUTOPILOT_S3_ENDPOINT_URL``, ``MANGA_AUTOPILOT_S3_BUCKET``,
      ``MANGA_AUTOPILOT_S3_REGION``, ``MANGA_AUTOPILOT_S3_ACCESS_KEY_ID``,
      ``MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY``.
    """
    policy = create_artifact_access_policy_from_env()

    if policy.mode == "public":
        return LocalArtifactUrlSigner()

    if policy.mode == "signed":
        bucket = os.environ.get("MANGA_AUTOPILOT_S3_BUCKET", "")
        if not bucket:
            # Fall back to local signer when no S3 bucket is configured.
            return LocalArtifactUrlSigner()
        return S3CompatibleArtifactUrlSigner(
            endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
            bucket=bucket,
            region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
            access_key_id=os.environ.get("MANGA_AUTOPILOT_S3_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY"),
        )

    # Private mode: no signer.
    return None
