"""The injected notifier: Telegram when it is there, a printed line when not.

Telegram lives in a different project ($HOME/.claude), so it is strictly
optional — aicp keeps working for anyone who only has this repo. The script
path comes from ``AICP_TG_SEND`` (an environment variable ONLY — never from
the config file, which is why nothing here touches .aicprc), and no failure on
that path may ever reach the caller: the notifier is wired into the runner as
a plain ``NotifyFn`` and a raised exception there would take down a run that
had already succeeded.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from aicp import notify as notify_module
from aicp.notify import notify


@pytest.fixture
def recording_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Factory: install a fake tg-send.sh at ``AICP_TG_SEND``.

    Returns ``(record_path, install)``; ``install(exit_code)`` writes the
    script and points the environment variable at it.
    """
    record = tmp_path / "sent.log"

    def install(exit_code: int = 0) -> Path:
        script = tmp_path / "tg-send.sh"
        script.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$1" "$2" >> "{record}"\nexit {exit_code}\n',
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("AICP_TG_SEND", str(script))
        return script

    return record, install


posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the /bin/sh stub script is POSIX-only"
)


# ── the send path ────────────────────────────────────────────────────────────


@posix_only
def test_sends_through_the_script_when_it_is_available(recording_script, capsys) -> None:
    record, install = recording_script
    install()

    notify("hello from aicp")

    assert record.read_text(encoding="utf-8").splitlines() == [
        "send",
        "hello from aicp",
    ]
    assert "telegram unavailable" not in capsys.readouterr().out


@posix_only
def test_a_script_that_fails_degrades_to_the_printed_line(
    recording_script, capsys
) -> None:
    _, install = recording_script
    install(exit_code=1)

    notify("hello")

    assert "telegram unavailable" in capsys.readouterr().out


# ── the fallback path ────────────────────────────────────────────────────────


def test_a_missing_script_degrades_to_a_printed_line(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.setenv("AICP_TG_SEND", str(tmp_path / "nonexistent.sh"))

    notify("hello")

    out = capsys.readouterr().out
    assert "telegram unavailable" in out
    assert "hello" not in out, "the fallback announces the failure, not the payload"


def test_a_directory_where_a_script_should_be_never_raises(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    monkeypatch.setenv("AICP_TG_SEND", str(tmp_path))

    notify("hello")  # must not raise

    assert "telegram unavailable" in capsys.readouterr().out


def test_the_default_path_is_under_home_and_is_not_the_developers_real_one(
    monkeypatch: pytest.MonkeyPatch, pinned_environment: Path
) -> None:
    """No AICP_TG_SEND set: the default is $HOME/.claude/scripts/tg-send.sh,
    resolved at call time so a test's fake HOME actually applies."""
    monkeypatch.delenv("AICP_TG_SEND", raising=False)
    assert notify_module.tg_send_script() == (
        pinned_environment / ".claude" / "scripts" / "tg-send.sh"
    )


def test_the_script_path_is_read_from_the_environment_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Environment variable only — never a config value, and never cached at
    import, or a run that sets it would be ignored."""
    monkeypatch.setenv("AICP_TG_SEND", str(tmp_path / "first.sh"))
    assert notify_module.tg_send_script() == tmp_path / "first.sh"
    monkeypatch.setenv("AICP_TG_SEND", str(tmp_path / "second.sh"))
    assert notify_module.tg_send_script() == tmp_path / "second.sh"


# ── contract compatibility ───────────────────────────────────────────────────


def test_notify_is_notifyfn_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """``NotifyFn = Callable[[str], None]``: one positional string, no
    meaningful return, no exception — whatever the environment looks like."""
    from aicp.contracts import NotifyFn

    monkeypatch.setenv("AICP_TG_SEND", str(tmp_path / "nope.sh"))
    fn: NotifyFn = notify
    assert fn("a message") is None
    capsys.readouterr()
