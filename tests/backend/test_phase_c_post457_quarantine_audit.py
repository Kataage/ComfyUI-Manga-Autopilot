"""Never-merge, test-only Phase C audit after #457.

These are evidence-preservation probes. The source code is intentionally unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import manga_autopilot.repositories.work_lifecycle as lifecycle
from manga_autopilot.repositories import WorkLifecycleRepository, WorkRecoveryError


@pytest.mark.parametrize(
    "corrupted_field",
    ["protocol_version", "work_id", "source", "destination"],
)
def test_post457_invalid_prepared_receipt_does_not_allow_new_quarantine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupted_field: str,
) -> None:
    """A prepared protocol conflict must not be bypassed with a new receipt.

    Simulate the real crash window: the prepared receipt was persisted, but
    the directory move failed, leaving original staging evidence available.
    """
    repository = WorkLifecycleRepository(tmp_path)
    work_id = "work_post457_prepared_owner"
    staging = tmp_path / "works" / f".creating-{work_id}"
    staging.mkdir()
    evidence = staging / "evidence.bin"
    evidence.write_bytes(b"irreplaceable original staging evidence")
    original_move = lifecycle._durable_move_directory

    def fail_move(source: Path, destination: Path) -> None:
        raise OSError("controlled failure after prepared receipt")

    monkeypatch.setattr(lifecycle, "_durable_move_directory", fail_move)
    with pytest.raises(OSError, match="controlled failure"):
        repository.quarantine_staging_work(work_id)

    prepared = repository.inspect_quarantine()
    assert len(prepared) == 1
    assert prepared[0].kind == "QUARANTINE_INCOMPLETE"
    receipt = prepared[0].path
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["state"] == "prepared"
    assert not receipt.with_name(payload["destination"]).exists()

    if corrupted_field == "protocol_version":
        payload["protocol_version"] = 999
    elif corrupted_field == "work_id":
        payload["work_id"] = "work_post457_foreign"
    elif corrupted_field == "source":
        payload["source"] = ".creating-work_post457_foreign"
    else:
        payload["destination"] = "foreign-evidence-directory"

    tampered = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    receipt.write_bytes(tampered)
    findings = repository.inspect_quarantine()
    assert len(findings) == 1
    assert findings[0].kind == "QUARANTINE_INVALID"
    assert findings[0].recommended_action is None
    assert any(
        item.kind == "QUARANTINE_INVALID" for item in repository.scan_recovery()
    )

    quarantine_root = receipt.parent
    names_before = sorted(item.name for item in quarantine_root.iterdir())
    catalog_before = repository.list_works()
    staging_before = evidence.read_bytes()
    monkeypatch.setattr(lifecycle, "_durable_move_directory", original_move)

    # Expected fail-closed behavior: an unresolved invalid prepared receipt
    # must prevent moving this Work's staging evidence to a second directory.
    with pytest.raises(WorkRecoveryError):
        repository.quarantine_staging_work(work_id)

    assert staging.is_dir()
    assert evidence.read_bytes() == staging_before
    assert receipt.read_bytes() == tampered
    assert sorted(item.name for item in quarantine_root.iterdir()) == names_before
    assert repository.list_works() == catalog_before


def test_post457_valid_prepared_receipt_recovers_same_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: legitimate interrupted quarantine stays retryable."""
    repository = WorkLifecycleRepository(tmp_path)
    work_id = "work_post457_prepared_valid"
    staging = tmp_path / "works" / f".creating-{work_id}"
    staging.mkdir()
    (staging / "evidence.bin").write_bytes(b"legitimate recoverable evidence")
    original_move = lifecycle._durable_move_directory

    def fail_move(source: Path, destination: Path) -> None:
        raise OSError("controlled failure after prepared receipt")

    monkeypatch.setattr(lifecycle, "_durable_move_directory", fail_move)
    with pytest.raises(OSError, match="controlled failure"):
        repository.quarantine_staging_work(work_id)

    findings = repository.inspect_quarantine()
    assert len(findings) == 1
    assert findings[0].kind == "QUARANTINE_INCOMPLETE"
    receipt = findings[0].path
    expected_destination = receipt.with_name(
        receipt.name[: -len(".recovery.json")]
    )
    monkeypatch.setattr(lifecycle, "_durable_move_directory", original_move)
    assert repository.quarantine_staging_work(work_id) == expected_destination
    assert not staging.exists()
    assert (expected_destination / "evidence.bin").read_bytes() == (
        b"legitimate recoverable evidence"
    )
    assert repository.inspect_quarantine()[0].kind == "QUARANTINE_COMPLETE"
    assert repository.scan_recovery() == ()
