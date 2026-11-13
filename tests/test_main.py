import asyncio
import logging
import sys

import pytest

from wordlebot import __main__ as entry


def run_main(monkeypatch, tmp_path, exc):
    def fake_run(coro):
        coro.close()  # never awaited; avoid a RuntimeWarning
        raise exc

    monkeypatch.setattr(asyncio, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["wordlebot", "--config", str(tmp_path / "nope.toml"), "sync"])
    entry.main()


def test_keyboard_interrupt_is_a_quiet_clean_exit(monkeypatch, tmp_path, caplog, capsys):
    with caplog.at_level(logging.INFO, logger="wordlebot"):
        run_main(monkeypatch, tmp_path, KeyboardInterrupt())  # returns => exit status 0
    assert [r.getMessage() for r in caplog.records if r.name == "wordlebot"] == ["Shutting down"]
    assert all(r.exc_info is None for r in caplog.records)
    assert "Traceback" not in capsys.readouterr().err


def test_other_errors_still_propagate(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError):
        run_main(monkeypatch, tmp_path, RuntimeError("boom"))
