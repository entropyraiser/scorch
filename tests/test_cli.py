"""CLI wiring for Scorch commands."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scorch.cli import _configure_logging, main, session_time
from scorch.snapshot import DownloadError


def test_download_symbols_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "scorch.cli.download_symbols", lambda out_dir: out_dir or tmp_path
    )
    code = main(["download-symbols", "--broker", "firstock", "--out", str(tmp_path)])
    assert code == 0
    assert f"wrote files under {tmp_path}" in capsys.readouterr().out


def test_download_symbols_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_out: Path | None) -> Path:
        raise DownloadError("boom")

    monkeypatch.setattr("scorch.cli.download_symbols", fail)
    code = main(["download-symbols", "--broker", "firstock", "--out", str(tmp_path)])
    assert code == 1
    assert "error: boom" in capsys.readouterr().err


def test_download_kite_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "scorch.cli.download_instruments", lambda out_dir: out_dir or tmp_path
    )
    code = main(["download-symbols", "--broker", "kite", "--out", str(tmp_path)])
    assert code == 0
    assert f"wrote files under {tmp_path}" in capsys.readouterr().out


def test_download_kite_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_out: Path | None) -> Path:
        raise DownloadError("boom")

    monkeypatch.setattr("scorch.cli.download_instruments", fail)
    code = main(["download-symbols", "--broker", "kite", "--out", str(tmp_path)])
    assert code == 1
    assert "error: boom" in capsys.readouterr().err


def test_unknown_broker_exits() -> None:
    with pytest.raises(SystemExit):
        main(["download-symbols", "--broker", "unknown"])


def test_missing_command_exits() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_missing_broker_exits() -> None:
    with pytest.raises(SystemExit):
        main(["download-symbols"])


def test_missing_out_exits() -> None:
    with pytest.raises(SystemExit):
        main(["download-symbols", "--broker", "firstock"])


def test_download_symbols_reports_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_out: Path | None) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr("scorch.cli.download_symbols", fail)
    code = main(["download-symbols", "--broker", "firstock", "--out", str(tmp_path)])
    assert code == 1
    assert "error: disk full" in capsys.readouterr().err


def test_session_time_uses_the_configured_zone() -> None:
    moment = datetime(2026, 8, 30, 20, 0, tzinfo=UTC)
    converted = session_time(moment.timestamp())
    assert converted.tm_year == 2026
    assert converted.tm_mon == 8
    assert converted.tm_mday == 31
    assert converted.tm_hour == 1
    assert converted.tm_min == 30


def test_configure_logging_stamps_stderr_in_the_session_zone() -> None:
    _configure_logging()
    stamped = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.StreamHandler)
        and getattr(handler, "stream", None) is sys.stderr
        and handler.formatter is not None
    ]
    assert stamped
    assert stamped[-1].formatter is not None
    assert stamped[-1].formatter.converter is session_time


def test_broker_without_a_downloader_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("scorch.cli._downloaders", lambda: {"firstock": None})
    code = main(["list-brokers"])
    assert code == 1
    assert "no downloader" in capsys.readouterr().err


def test_list_brokers_prints_the_universe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["list-brokers"]) == 0
    assert capsys.readouterr().out.split() == ["firstock", "kite"]


def test_calendar_day_prints_eight_digits(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["calendar-day"]) == 0
    assert len(capsys.readouterr().out.strip()) == 8
