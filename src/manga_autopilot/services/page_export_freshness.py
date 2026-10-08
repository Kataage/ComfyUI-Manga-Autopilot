"""Read-only commit-ordered freshness for immutable v2 Page PNG Artifacts.

READY describes file publication, not whether the saved Page still matches.
All checks use the caller's one Work SQLite snapshot. Never rewrite old
Artifact rows or clear invalidations to make history appear current.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from manga_autopilot.repositories.artifacts import (
    VERIFIED_PAGE_RENDER_OPERATION,
    verify_page_render_attestation,
)


def page_png_freshness(
    connection: sqlite3.Connection, artifact: dict[str, Any],
) -> dict[str, Any]:
    """Return conservative CURRENT / STALE / UNVERIFIED provenance metadata.

    This is a read model, not a durable change to Artifact.status. A successful
    Page render uses a canonical SHA256 dependency_fingerprint; records without
    that contract cannot be proven fresh and must not become current by default.
    """
    page_id = artifact["scope_id"]
    published = artifact["created_commit_seq"]
    fingerprint = artifact["dependency_fingerprint"]
    unverified = {
        "freshness": "UNVERIFIED",
        "is_current": False,
        "freshness_reason": "source_provenance_unavailable",
        "stale_since_commit_seq": None,
    }
    if (
        type(published) is not int or published < 1
        or not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(ch not in "0123456789abcdef" for ch in fingerprint)
    ):
        return unverified

    # A digest-shaped string proves nothing about the source of the PNG.
    # Only a Work commit minted by the source-guarded exporter after its
    # BEGIN IMMEDIATE snapshot check can certify this Artifact as CURRENT.
    # Historic/generic/imported rows remain retrievable but UNVERIFIED.
    attestation = connection.execute(
        """SELECT operation_type, actor_type, reason
           FROM commits WHERE commit_seq = ?""",
        (published,),
    ).fetchone()
    if (
        attestation is None
        or attestation["operation_type"] != VERIFIED_PAGE_RENDER_OPERATION
        or attestation["actor_type"] != "system"
        or not verify_page_render_attestation(attestation["reason"], artifact)
    ):
        return unverified

    page = connection.execute(
        """SELECT p.archived_at, p.layout_instance_id
           FROM pages p WHERE p.id = ?""",
        (page_id,),
    ).fetchone()
    if page is None or page["layout_instance_id"] is None:
        return unverified
    if page["archived_at"] is not None:
        return {
            "freshness": "STALE",
            "is_current": False,
            "freshness_reason": "page_archived",
            "stale_since_commit_seq": None,
        }

    # Chronology, not resolved_commit_seq: resolving a later invalidation must
    # NEVER make an earlier immutable PNG current again.
    invalidation = connection.execute(
        """SELECT MIN(created_commit_seq) AS first_commit
           FROM invalidations
           WHERE target_type IN ('page_render', 'page_export')
             AND target_id = ?
             AND created_commit_seq > ?""",
        (page_id, published),
    ).fetchone()["first_commit"]
    if invalidation is not None:
        return {
            "freshness": "STALE",
            "is_current": False,
            "freshness_reason": "page_inputs_changed",
            "stale_since_commit_seq": invalidation,
        }

    # The exporter may use a sole unpinned READY candidate if an *active*
    # Panel has no selection. A later Candidate publication makes that choice
    # ambiguous without a Panel revision. Archived Panels, however, are
    # excluded from #337's compositing inputs and must not stale active PNGs
    # merely because their unrelated Candidate history grows. All checks use
    # this same Work SQLite read snapshot as Page/archive and invalidations.
    ambiguous = connection.execute(
        """SELECT 1 FROM panels p
           JOIN artifacts a ON a.scope_type = 'panel' AND a.scope_id = p.id
           WHERE p.page_id = ? AND p.archived_at IS NULL
             AND p.selected_candidate_id IS NULL
             AND a.artifact_type = 'panel_candidate'
             AND a.status = 'READY' AND a.archived_at IS NULL
             AND a.created_commit_seq > ?
           LIMIT 1""",
        (page_id, published),
    ).fetchone()
    if ambiguous is not None:
        return {
            "freshness": "STALE",
            "is_current": False,
            "freshness_reason": "candidate_choice_changed",
            "stale_since_commit_seq": None,
        }
    return {
        "freshness": "CURRENT",
        "is_current": True,
        "freshness_reason": None,
        "stale_since_commit_seq": None,
    }


__all__ = ["page_png_freshness"]
