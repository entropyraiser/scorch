"""Tests for publishing a snapshot without touching the live directory early."""

from __future__ import annotations

import fcntl
import json
import threading
import time
from pathlib import Path

import pytest

from scorch.snapshot import (
    DownloadError,
    atomic_write,
    continuity_check,
    continuity_rows,
    describe_baseline,
    load_baseline,
    overlap_continuity,
    publish_snapshot,
)


def test_publish_replaces_directory_and_cleans_up(tmp_path: Path) -> None:
    target = tmp_path / "day"
    target.mkdir()
    (target / "old.csv").write_bytes(b"old")
    publish_snapshot(target, {"new.csv": b"new", "manifest.json": b"{}"})
    assert (target / "new.csv").read_bytes() == b"new"
    assert not (target / "old.csv").exists()
    assert not (tmp_path / "day.partial").exists()
    assert not (tmp_path / "day.bak").exists()


def test_publish_failure_leaves_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "day"
    target.mkdir()
    (target / "keep.txt").write_bytes(b"old")
    calls = {"n": 0}
    real = atomic_write

    def flaky(path: Path, payload: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk")
        real(path, payload)

    monkeypatch.setattr("scorch.snapshot.atomic_write", flaky)
    with pytest.raises(OSError, match="disk"):
        publish_snapshot(target, {"a.csv": b"a", "b.csv": b"b"})
    assert (target / "keep.txt").read_bytes() == b"old"
    assert not (target / "a.csv").exists()
    assert not (tmp_path / "day.partial").exists()
    assert not (tmp_path / "day.bak").exists()


def test_publish_restores_backup_left_by_an_interrupted_swap(tmp_path: Path) -> None:
    backup = tmp_path / "day.bak"
    backup.mkdir()
    (backup / "keep.txt").write_bytes(b"old")
    target = tmp_path / "day"
    publish_snapshot(target, {"new.csv": b"new"})
    assert (target / "new.csv").read_bytes() == b"new"
    assert not (target / "keep.txt").exists()
    assert not backup.exists()


def test_publish_rejects_nested_names(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="refusing to publish"):
        publish_snapshot(tmp_path / "day", {"nested/file.csv": b"x"})
    assert not (tmp_path / "day.lock").exists()


def test_before_swap_sees_snapshot_restored_from_backup(tmp_path: Path) -> None:
    backup = tmp_path / "day.bak"
    backup.mkdir()
    (backup / "manifest.json").write_text('{"old": true}')
    seen: dict[str, str] = {}

    def before_swap() -> None:
        seen["body"] = (tmp_path / "day" / "manifest.json").read_text()

    publish_snapshot(tmp_path / "day", {"new.csv": b"new"}, before_swap=before_swap)
    assert seen["body"] == '{"old": true}'
    assert (tmp_path / "day" / "new.csv").read_bytes() == b"new"
    assert not (tmp_path / "day" / "manifest.json").exists()


def test_before_swap_error_keeps_restored_snapshot(tmp_path: Path) -> None:
    backup = tmp_path / "day.bak"
    backup.mkdir()
    (backup / "keep.txt").write_bytes(b"old")

    def before_swap() -> None:
        raise DownloadError("shift")

    with pytest.raises(DownloadError, match="shift"):
        publish_snapshot(tmp_path / "day", {"new.csv": b"new"}, before_swap=before_swap)
    assert (tmp_path / "day" / "keep.txt").read_bytes() == b"old"
    assert not (tmp_path / "day" / "new.csv").exists()


def test_publish_waits_while_lock_is_held(tmp_path: Path) -> None:
    target = tmp_path / "day"
    lock_file = (tmp_path / "day.lock").open("a")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    finished = threading.Event()

    def run() -> None:
        publish_snapshot(target, {"new.csv": b"new"})
        finished.set()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        time.sleep(0.2)
        assert not finished.is_set()
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert finished.is_set()
    assert (target / "new.csv").read_bytes() == b"new"


def test_continuity_allows_the_40_percent_boundary() -> None:
    baseline = {"segments": [{"segment": "NFO", "rows": 10000}]}
    deltas = continuity_check(baseline, {"NFO": 14000})
    assert deltas == [
        {
            "segment": "NFO",
            "previous_rows": 10000,
            "rows": 14000,
            "relative_change": 0.4,
        }
    ]
    with pytest.raises(DownloadError, match="row count changed"):
        continuity_check(baseline, {"NFO": 14001})
    kept = continuity_check(baseline, {"NFO": 6000})
    assert kept[0]["relative_change"] == -0.4
    with pytest.raises(DownloadError, match="row count changed"):
        continuity_check(baseline, {"NFO": 5999})


def test_continuity_rejects_a_missing_segment() -> None:
    baseline = {"segments": [{"segment": "NFO", "rows": 10}]}
    with pytest.raises(DownloadError, match="missing from the new snapshot"):
        continuity_check(baseline, {})


def test_overlap_continuity_skips_a_manifest_without_overlaps() -> None:
    assert overlap_continuity({"segments": []}, [{"also_in": "NFO", "rows": 1}]) == []


def test_overlap_continuity_rejects_a_large_move() -> None:
    baseline = {"overlaps": [{"also_in": "NFO", "rows": 1000}]}
    with pytest.raises(DownloadError, match="Indices overlap with NFO"):
        overlap_continuity(baseline, [{"also_in": "NFO", "rows": 100}])


def test_load_baseline_prefers_same_directory_over_yesterday(tmp_path: Path) -> None:
    day = tmp_path / "20260301"
    day.mkdir()
    (day / "manifest.json").write_text(json.dumps({"segments": [{"segment": "NSE"}]}))
    previous = tmp_path / "20260228"
    previous.mkdir()
    (previous / "manifest.json").write_text(json.dumps({"from": "yesterday"}))
    source, body = load_baseline(day)
    assert source == "same_directory"
    assert body == {"segments": [{"segment": "NSE"}]}
    described = describe_baseline(source, day, [], [])
    assert described is not None
    assert described["calendar_day"] == "2026-03-01"


def test_load_baseline_reads_previous_calendar_day(tmp_path: Path) -> None:
    day = tmp_path / "20260301"
    previous = tmp_path / "20260228"
    previous.mkdir()
    (previous / "manifest.json").write_text(json.dumps({"from": "yesterday"}))
    source, body = load_baseline(day)
    assert source == "previous_day"
    assert body == {"from": "yesterday"}
    described = describe_baseline(source, day, [], [])
    assert described is not None
    assert described["calendar_day"] == "2026-02-28"


def test_load_baseline_ignores_a_non_date_directory(tmp_path: Path) -> None:
    source, body = load_baseline(tmp_path / "custom")
    assert source is None
    assert body is None


def test_continuity_rows_ignores_the_retired_indices_file() -> None:
    baseline = {
        "segments": [
            {"segment": "NSE", "rows": 10},
            {"segment": "Indices", "rows": 99},
        ]
    }
    compared, current = continuity_rows(baseline, {"NSE": 10}, 10)
    assert compared is not None
    assert [item["segment"] for item in compared["segments"]] == ["NSE"]
    deltas = continuity_check(compared, current)
    assert [item["segment"] for item in deltas] == ["NSE"]


def test_continuity_rows_compares_an_old_kite_total() -> None:
    baseline = {"segments": [{"segment": "ALL", "rows": 50}]}
    _compared, current = continuity_rows(baseline, {"NSE": 40, "NFO": 10}, 50)
    assert current == {"ALL": 50}
    deltas = continuity_check(baseline, current)
    assert deltas[0]["relative_change"] == 0


def test_load_baseline_rejects_a_broken_manifest(tmp_path: Path) -> None:
    target = tmp_path / "day"
    target.mkdir()
    (target / "manifest.json").write_text("{")
    with pytest.raises(DownloadError, match="unreadable manifest"):
        load_baseline(target)
