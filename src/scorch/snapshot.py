"""Publish a dated snapshot only after every file is ready.

A failed download leaves the previous directory untouched. Readers see either
the old snapshot or the new one, and in both cases the manifest matches the
CSVs beside it. One publish holds an exclusive lock on `<target>.lock` so a
second run of the same directory waits instead of sharing `.partial` and `.bak`.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("Asia/Kolkata")
MANIFEST_SCHEMA_VERSION = 3
# Firstock published the Indices file beside the other segments. The normalized
# book omits it, because those rows repeat NFO and BFO.
_RETIRED_SEGMENTS = frozenset({"Indices"})
# 40.00% — integer basis points so the boundary does not depend on binary floats.
MAX_RELATIVE_CHANGE_BP = 4000

logger = logging.getLogger(__name__)

_DATE_NAME = re.compile(r"^\d{8}$")


class DownloadError(RuntimeError):
    """A download failed validation or the HTTP GET failed after retries."""


def exchange_day(now: datetime | None = None) -> str:
    """Calendar day in India, which is the NSE/BSE session date."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(EXCHANGE_TZ).strftime("%Y-%m-%d")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    _fsync_dir(path.parent)


def _recover_interrupted_publish(target: Path, backup: Path) -> None:
    if backup.exists() and target.exists():
        shutil.rmtree(backup)
    elif backup.exists():
        backup.rename(target)


def _reject_bad_names(files: Mapping[str, bytes]) -> None:
    for name in files:
        if name != Path(name).name or name in {"", ".", ".."}:
            raise DownloadError(f"refusing to publish {name}")


@contextmanager
def _exclusive_publish(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(f"{target.name}.lock")
    handle = lock_path.open("a")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def publish_snapshot(
    target: Path,
    files: Mapping[str, bytes],
    *,
    before_swap: Callable[[], None] | None = None,
) -> None:
    """Swap `files` into `target` as one snapshot.

    Nothing under `target` changes until every file is staged. A crash while
    the previous directory is moved aside leaves `<target>.bak`, and the next
    publish puts that directory back before replacing it. `before_swap` runs
    under the lock after that recovery and before the new files are staged, so
    a continuity check sees the snapshot that is about to be replaced.
    """
    _reject_bad_names(files)
    if not files and before_swap is None:
        raise DownloadError("refusing to publish an empty snapshot")

    staging = target.with_name(f"{target.name}.partial")
    backup = target.with_name(f"{target.name}.bak")
    with _exclusive_publish(target):
        _recover_interrupted_publish(target, backup)
        if before_swap is not None:
            before_swap()
        if not files:
            raise DownloadError("refusing to publish an empty snapshot")
        _reject_bad_names(files)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        try:
            for name, payload in files.items():
                atomic_write(staging / name, payload)
            _fsync_dir(staging)
            if target.exists():
                target.rename(backup)
            staging.rename(target)
            _fsync_dir(target.parent)
        except Exception:
            if not target.exists() and backup.exists():
                backup.rename(target)
            raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(backup)


def _directory_day(name: str) -> datetime | None:
    if not _DATE_NAME.fullmatch(name):
        return None
    try:
        return datetime.strptime(name, "%Y%m%d")
    except ValueError:
        return None


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise DownloadError(f"unreadable manifest {path}: {error}") from error
    try:
        body = json.loads(text)
    except json.JSONDecodeError as error:
        raise DownloadError(f"unreadable manifest {path}: {error}") from error
    if not isinstance(body, dict):
        raise DownloadError(f"manifest {path} is not an object")
    return body


def load_baseline(target: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Manifest this publish should be compared with.

    A same-day rerun uses the manifest already in `target`. The first publish
    of a dated directory uses the previous calendar day's sibling, when that
    snapshot exists. Returns `(source, body)` where source is
    `"same_directory"`, `"previous_day"`, or None.
    """
    own = target / "manifest.json"
    if own.is_file():
        return "same_directory", _read_manifest(own)
    parsed = _directory_day(target.name)
    if parsed is not None:
        day = parsed.date()
        sibling = target.with_name((day - timedelta(days=1)).strftime("%Y%m%d"))
        previous = sibling / "manifest.json"
        if previous.is_file():
            return "previous_day", _read_manifest(previous)
    return None, None


def _segment_rows(manifest: Mapping[str, Any]) -> dict[str, int]:
    rows: dict[str, int] = {}
    segments = manifest.get("segments")
    if not isinstance(segments, list):
        return rows
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        name = segment.get("segment")
        count = segment.get("rows")
        if (
            isinstance(name, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
        ):
            rows[name] = count
    return rows


def _exceeds(previous: int, current: int) -> bool:
    if previous <= 0:
        return current != previous
    return abs(current - previous) * 10000 > previous * MAX_RELATIVE_CHANGE_BP


def _signed_change(previous: int, current: int) -> float:
    if previous == 0:
        return 0.0
    return round((current - previous) / previous, 6)


def _limit_text() -> str:
    return f"±{MAX_RELATIVE_CHANGE_BP / 100:.0f}%"


def continuity_check(
    baseline: Mapping[str, Any] | None,
    current_rows: Mapping[str, int],
) -> list[dict[str, object]]:
    """Raise when a segment row count moves more than the allowed fraction."""
    if baseline is None:
        return []
    previous = _segment_rows(baseline)
    deltas: list[dict[str, object]] = []
    for segment, old in previous.items():
        if segment not in current_rows:
            raise DownloadError(
                f"{segment} is missing from the new snapshot; "
                f"the previous one has {old} rows"
            )
        rows = current_rows[segment]
        if _exceeds(old, rows):
            raise DownloadError(
                f"{segment} row count changed from {old} to {rows} "
                f"({_signed_change(old, rows):+.1%}); limit is {_limit_text()}"
            )
        deltas.append(
            {
                "segment": segment,
                "previous_rows": old,
                "rows": rows,
                "relative_change": _signed_change(old, rows),
            }
        )
    return deltas


def continuity_rows(
    baseline: Mapping[str, Any] | None,
    exchange_rows: Mapping[str, int],
    total_rows: int,
) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """Choose the row counts a new book should be compared with.

    A Kite manifest from before normalization has one ALL segment, which is
    the whole dump. A Firstock manifest from that era counts the Indices file,
    which this book no longer publishes.
    """
    if baseline is None:
        return None, dict(exchange_rows)
    previous = _segment_rows(baseline)
    if set(previous) == {"ALL"}:
        return dict(baseline), {"ALL": total_rows}
    retired = previous.keys() & _RETIRED_SEGMENTS
    if not retired:
        return dict(baseline), dict(exchange_rows)
    segments = baseline.get("segments")
    kept: list[Any] = []
    if isinstance(segments, list):
        kept = [
            item
            for item in segments
            if not (isinstance(item, dict) and item.get("segment") in _RETIRED_SEGMENTS)
        ]
    filtered = dict(baseline)
    filtered["segments"] = kept
    return filtered, dict(exchange_rows)


def _overlap_rows(manifest: Mapping[str, Any]) -> dict[str, int] | None:
    if "overlaps" not in manifest:
        return None
    raw = manifest.get("overlaps")
    if not isinstance(raw, list):
        return None
    rows: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        also_in = item.get("also_in")
        count = item.get("rows")
        if (
            isinstance(also_in, str)
            and isinstance(count, int)
            and not isinstance(count, bool)
        ):
            rows[also_in] = count
    return rows


def overlap_continuity(
    baseline: Mapping[str, Any] | None,
    overlaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, object]]:
    """Raise when an Indices overlap moves more than the allowed fraction.

    A baseline written before overlaps were recorded has no `overlaps` key,
    and that comparison is skipped.
    """
    if baseline is None:
        return []
    previous = _overlap_rows(baseline)
    if previous is None:
        return []
    current: dict[str, int] = {}
    for item in overlaps:
        also_in = item.get("also_in")
        count = item.get("rows")
        if isinstance(also_in, str) and isinstance(count, int):
            current[also_in] = count
    deltas: list[dict[str, object]] = []
    for also_in, old in previous.items():
        if also_in not in current:
            raise DownloadError(
                f"Indices overlap with {also_in} is missing; "
                f"the previous snapshot has {old} rows"
            )
        rows = current[also_in]
        if _exceeds(old, rows):
            raise DownloadError(
                f"Indices overlap with {also_in} changed from {old} to {rows} "
                f"({_signed_change(old, rows):+.1%}); limit is {_limit_text()}"
            )
        deltas.append(
            {
                "also_in": also_in,
                "previous_rows": old,
                "rows": rows,
                "relative_change": _signed_change(old, rows),
            }
        )
    return deltas


def describe_baseline(
    source: str | None,
    target: Path,
    segment_deltas: Sequence[Mapping[str, Any]],
    overlap_deltas: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """JSON object describing which snapshot the continuity check used."""
    if source is None:
        return None
    calendar_day = None
    parsed = _directory_day(target.name)
    if parsed is not None:
        day = parsed.date()
        if source == "previous_day":
            calendar_day = (day - timedelta(days=1)).isoformat()
        elif source == "same_directory":
            calendar_day = day.isoformat()
    return {
        "source": source,
        "calendar_day": calendar_day,
        "segments": list(segment_deltas),
        "overlaps": list(overlap_deltas),
    }


def log_continuity(
    log: logging.Logger,
    source: str | None,
    segment_deltas: Sequence[Mapping[str, Any]],
    overlap_deltas: Sequence[Mapping[str, Any]],
) -> None:
    if source is None:
        log.info("no previous snapshot to compare")
        return
    for delta in segment_deltas:
        log.info(
            "%s: %s rows, previous %s (%+.1f%%)",
            delta["segment"],
            delta["rows"],
            delta["previous_rows"],
            float(delta["relative_change"]) * 100,
        )
    for delta in overlap_deltas:
        log.info(
            "Indices overlap %s: %s rows, previous %s (%+.1f%%)",
            delta["also_in"],
            delta["rows"],
            delta["previous_rows"],
            float(delta["relative_change"]) * 100,
        )
