"""Artifact store foundation for Manga Autopilot.

Provides an interface for storing generated images and other artifacts,
with local and S3-compatible implementations.

The local store is used in standard CI.  The S3-compatible store
requires the ``boto3`` optional dependency (``pip install -e ".[s3]"``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------- upload result


@dataclass(frozen=True)
class ArtifactUploadResult:
    """Result of uploading an artifact to a store."""

    artifact_key: str
    artifact_url: str | None = None
    artifact_path: str | None = None
    content_type: str = "image/png"
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------- store protocol


@runtime_checkable
class ArtifactStore(Protocol):
    """Protocol for artifact store implementations."""

    def upload_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str = "image/png",
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactUploadResult:
        """Upload bytes to the artifact store.

        Parameters
        ----------
        key:
            The artifact key (path-like string).
        data:
            The bytes to upload.
        content_type:
            MIME type of the artifact.
        metadata:
            Optional metadata headers.

        Returns
        -------
        ArtifactUploadResult
        """
        ...


# --------------------------------------------------- local artifact store


class LocalArtifactStore:
    """Artifact store that saves files to a local directory.

    Used in standard CI and for local development.

    Parameters
    ----------
    root:
        Root directory for artifact storage.
    public_base_url:
        Optional base URL for generating public artifact URLs.
        If ``None``, only ``artifact_path`` is returned.
    """

    def __init__(
        self,
        root: str | Path,
        public_base_url: str | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._public_base_url = public_base_url

    @property
    def root(self) -> Path:
        """Root directory for artifact storage."""
        return self._root

    def upload_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str = "image/png",
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactUploadResult:
        """Upload bytes to a local file.

        Parameters
        ----------
        key:
            The artifact key (relative path within root).
        data:
            The bytes to write.
        content_type:
            MIME type (stored as metadata).
        metadata:
            Optional metadata headers.

        Raises
        ------
        ValueError
            If the key contains path traversal or is empty.
        """
        _validate_artifact_key(key)

        dest = self._root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

        url: str | None = None
        if self._public_base_url:
            base = self._public_base_url.rstrip("/")
            url = f"{base}/{key}"

        return ArtifactUploadResult(
            artifact_key=key,
            artifact_url=url,
            artifact_path=str(dest),
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else {},
        )


# ----------------------------------------------- S3-compatible artifact store


class S3CompatibleArtifactStore:
    """Artifact store for S3-compatible services (AWS S3, Cloudflare R2).

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
    public_base_url:
        Optional public base URL for generating artifact URLs.
    """

    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        bucket: str,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        public_base_url: str | None = None,
    ) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for S3 artifact store. "
                "Install with: pip install -e '.[s3]'"
            ) from exc

        self._bucket = bucket
        self._public_base_url = public_base_url
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region_name,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def upload_bytes(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str = "image/png",
        metadata: Mapping[str, str] | None = None,
    ) -> ArtifactUploadResult:
        """Upload bytes to S3-compatible storage.

        Parameters
        ----------
        key:
            The S3 object key.
        data:
            The bytes to upload.
        content_type:
            MIME type.
        metadata:
            Optional metadata headers.
        """
        _validate_artifact_key(key)

        extra: dict[str, Any] = {"ContentType": content_type}
        if metadata:
            extra["Metadata"] = dict(metadata)

        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            **extra,
        )

        url: str | None = None
        if self._public_base_url:
            base = self._public_base_url.rstrip("/")
            url = f"{base}/{key}"

        return ArtifactUploadResult(
            artifact_key=key,
            artifact_url=url,
            content_type=content_type,
            size_bytes=len(data),
            metadata=dict(metadata) if metadata else {},
        )


# --------------------------------------------------- key builder


def build_panel_artifact_key(
    *,
    project_id: str,
    run_id: str,
    page_id: str,
    panel_id: str,
    candidate_id: str,
    filename: str = "output.png",
) -> str:
    """Build a safe artifact key for a panel image.

    Returns a key like::

        projects/{project_id}/runs/{run_id}/pages/{page_id}/panels/{panel_id}/{candidate_id}.png

    Raises ``ValueError`` if any part is empty or contains unsafe
    characters.
    """
    parts = {
        "project_id": project_id,
        "run_id": run_id,
        "page_id": page_id,
        "panel_id": panel_id,
        "candidate_id": candidate_id,
    }
    for name, value in parts.items():
        if not value:
            raise ValueError(f"{name} must not be empty")
        if ".." in value or "/" in value or "\\" in value:
            raise ValueError(f"{name} contains unsafe characters: {value!r}")

    # Sanitize filename.
    safe_filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)
    if not safe_filename:
        safe_filename = "output.png"

    return (
        f"projects/{project_id}/runs/{run_id}/pages/{page_id}"
        f"/panels/{panel_id}/{candidate_id}.png"
    )


def _validate_artifact_key(key: str) -> None:
    """Validate an artifact key for safety."""
    if not key:
        raise ValueError("artifact key must not be empty")
    if ".." in key:
        raise ValueError(f"artifact key contains path traversal: {key!r}")
    if key.startswith("/") or key.startswith("\\"):
        raise ValueError(f"artifact key must not be absolute: {key!r}")


# --------------------------------------------------- factory from env


def create_artifact_store_from_env() -> ArtifactStore:
    """Create an artifact store from environment variables.

    Reads ``MANGA_AUTOPILOT_ARTIFACT_STORE`` to determine the store
    type.  Defaults to ``"local"``.

    Environment variables:

    - ``MANGA_AUTOPILOT_ARTIFACT_STORE``: ``"local"`` or ``"s3"``
    - ``MANGA_AUTOPILOT_ARTIFACT_LOCAL_ROOT``: local store root
    - ``MANGA_AUTOPILOT_S3_ENDPOINT_URL``: S3 endpoint
    - ``MANGA_AUTOPILOT_S3_BUCKET``: S3 bucket name
    - ``MANGA_AUTOPILOT_S3_REGION``: AWS region
    - ``MANGA_AUTOPILOT_S3_ACCESS_KEY_ID``: access key
    - ``MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY``: secret key
    - ``MANGA_AUTOPILOT_S3_PUBLIC_BASE_URL``: public URL base
    """
    store_type = os.environ.get("MANGA_AUTOPILOT_ARTIFACT_STORE", "local")

    if store_type == "s3":
        return S3CompatibleArtifactStore(
            endpoint_url=os.environ.get("MANGA_AUTOPILOT_S3_ENDPOINT_URL"),
            bucket=os.environ.get("MANGA_AUTOPILOT_S3_BUCKET", ""),
            region_name=os.environ.get("MANGA_AUTOPILOT_S3_REGION"),
            access_key_id=os.environ.get("MANGA_AUTOPILOT_S3_ACCESS_KEY_ID"),
            secret_access_key=os.environ.get("MANGA_AUTOPILOT_S3_SECRET_ACCESS_KEY"),
            public_base_url=os.environ.get("MANGA_AUTOPILOT_S3_PUBLIC_BASE_URL"),
        )

    # Default: local store.
    root = os.environ.get("MANGA_AUTOPILOT_ARTIFACT_LOCAL_ROOT", "/tmp/manga-artifacts")
    return LocalArtifactStore(root=root)
