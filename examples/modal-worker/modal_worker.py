"""Modal worker MVP example for Manga Autopilot.

This module provides worker functions that follow the RemoteHTTPExecutor
contract for future Modal GPU execution.  Pure-Python helpers work
without the Modal SDK; the Modal app stub is only used when ``modal``
is installed.

CI runs the pure-Python path only — no real Modal GPU is required.
"""

from __future__ import annotations

import base64
import io
from typing import Any

try:
    import modal

    _HAS_MODAL = True
except ImportError:
    modal = None  # type: ignore[assignment]
    _HAS_MODAL = False


# --------------------------------------------------------- pure-Python helpers

REQUIRED_FIELDS = ("project_id", "panel_id", "prompt", "seed", "width", "height")


def validate_generate_panel_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a ``/v1/generate-panel`` request payload.

    Returns the validated payload on success.  Raises ``ValueError``
    with a descriptive message when required fields are missing or
    invalid.
    """

    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")

    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")

    # Type checks.
    seed = payload.get("seed")
    if not isinstance(seed, int):
        raise ValueError("'seed' must be an integer")

    width = payload.get("width")
    height = payload.get("height")
    if not isinstance(width, int) or width <= 0:
        raise ValueError("'width' must be a positive integer")
    if not isinstance(height, int) or height <= 0:
        raise ValueError("'height' must be a positive integer")

    return payload


def build_success_response(
    payload: dict[str, Any],
    image_base64: str,
    *,
    filename: str | None = None,
    seed: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a successful ``/v1/generate-panel`` response."""

    panel_id = payload.get("panel_id", "panel")
    return {
        "status": "completed",
        "filename": filename or f"{panel_id}.png",
        "image_base64": image_base64,
        "seed": seed or payload.get("seed", 0),
        "metadata": {
            "executor": "modal-worker-mvp",
            **(metadata or {}),
        },
    }


def build_error_response(message: str) -> dict[str, Any]:
    """Build an error ``/v1/generate-panel`` response."""

    return {
        "status": "error",
        "error": message,
    }


def make_placeholder_png_base64(width: int, height: int, seed: int) -> str:
    """Create a deterministic placeholder PNG as base64 (no PIL required)."""

    try:
        from PIL import Image

        r = (seed * 7) % 256
        g = (seed * 13) % 256
        b = (seed * 23) % 256
        img = Image.new("RGB", (width, height), (r, g, b))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        # Minimal 1x1 red PNG as absolute fallback (no PIL).
        import struct
        import zlib

        def _minimal_png() -> bytes:
            raw = b"\x00\xff\x00\x00"  # filter byte + RGB
            compressed = zlib.compress(raw)

            def _chunk(ctype: bytes, data: bytes) -> bytes:
                c = ctype + data
                return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

            ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
            return (
                b"\x89PNG\r\n\x1a\n"
                + _chunk(b"IHDR", ihdr)
                + _chunk(b"IDAT", compressed)
                + _chunk(b"IEND", b"")
            )

        return base64.b64encode(_minimal_png()).decode("ascii")


# -------------------------------------------------------- Modal app (optional)

if _HAS_MODAL:
    app = modal.App("manga-autopilot-worker")
    image = modal.Image.debian_slim().pip_install("pillow")

    @app.function(image=image, timeout=300)
    def generate_panel(payload: dict[str, Any]) -> dict[str, Any]:
        """Modal function entry point for panel generation.

        Receives a validated /v1/generate-panel payload and returns
        a response dict matching the RemoteHTTPExecutor contract.
        """

        try:
            validated = validate_generate_panel_payload(payload)
        except ValueError as exc:
            return build_error_response(str(exc))

        seed = validated["seed"]
        width = validated["width"]
        height = validated["height"]

        # Placeholder: replace with real ComfyUI / model execution.
        b64 = make_placeholder_png_base64(width, height, seed)
        return build_success_response(validated, b64)


# ----------------------------------------------------------- dry-run CLI

if __name__ == "__main__":

    sample_payload = {
        "project_id": "demo",
        "page_id": "",
        "panel_id": "panel_001_c00",
        "prompt": "1girl, masterpiece",
        "negative_prompt": "lowres, blurry",
        "seed": 42,
        "width": 64,
        "height": 64,
        "workflow_id": "anime_t2i_default",
        "metadata": {},
    }

    print("=== Modal Worker MVP Dry Run ===")
    print()

    # Validate.
    validated = validate_generate_panel_payload(sample_payload)
    print(f"Validated payload: {len(validated)} fields OK")

    # Generate placeholder image.
    b64 = make_placeholder_png_base64(64, 64, 42)
    print(f"Placeholder image: {len(b64)} chars base64")

    # Build success response.
    resp = build_success_response(validated, b64)
    print(f"Success response: status={resp['status']!r}, executor={resp['metadata']['executor']!r}")

    # Build error response.
    err = build_error_response("model not configured")
    print(f"Error response: status={err['status']!r}, error={err['error']!r}")

    # Check Modal availability.
    print(f"Modal SDK available: {_HAS_MODAL}")

    print()
    print("Done. No Modal GPU required.")
