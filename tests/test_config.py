"""Hardened ``.aicprc`` loader — the security-critical layer of the port.

Every test here is a port of an assertion in ``~/scripts/test_aicp.sh``'s
``.aicprc`` section (its "config file" block, roughly lines 1232-1665), whose
whole point is that a config file is DATA: it can set a documented knob and
it can do nothing else — not run a command, not name a command to run, not
name a path that gets written, renamed or deleted.

Two of these assert a NEGATIVE side effect rather than a return value (the
command-substitution marker and the ``$PATH`` arithmetic-injection case);
those are the ones that would actually go red if the loader ever regressed
into ``eval``/``exec`` territory, so they check the world, not the parser.

Local fixtures only — ``tests/conftest.py`` is a four-way contention point
and is never edited from here (see its docstring).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aicp.config import (
    DENYLIST,
    config_path,
    load_config,
    persist_key,
    resolve,
    resolve_cli_chain,
    timeout_bin,
)
from aicp.contracts import ROSTER

ROSTER_NAMES = tuple(c.name for c in ROSTER)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Factory: write *text* to a throwaway ``.aicprc`` and point
    ``AICP_CONFIG`` at it. Returns the path."""

    def _write(text: str) -> Path:
        path = tmp_path / "cfg.aicprc"
        path.write_text(text, encoding="utf-8")
        monkeypatch.setenv("AICP_CONFIG", str(path))
        return path

    return _write


@pytest.fixture
def marker_payload(tmp_path):
    """Factory: an executable shell script that touches a marker file.

    Returns ``(script_path, marker_path)``; the marker must never exist after
    a config load, because nothing in the loader may ever execute a value.
    """

    def _make(name: str) -> tuple[Path, Path]:
        marker = tmp_path / f"{name}.marker"
        script = tmp_path / f"{name}.sh"
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script, marker

    return _make


# ── precedence: environment > .aicprc > hardcoded default ────────────────────


def test_missing_config_file_changes_nothing():
    settings = resolve()
    assert load_config(settings.path) == {}
    assert settings.do_commit is True
    assert settings.do_push is True
    assert settings.lang == "en"
    assert tuple(settings.cli_chain) == ROSTER_NAMES


def test_config_value_applies_when_no_env_var_is_set(cfg):
    cfg("AICP_TIMEOUT_BASE=10\n")
    assert resolve().values["AICP_TIMEOUT_BASE"] == "10"


def test_env_var_beats_a_conflicting_config_key(cfg, monkeypatch):
    cfg("AICP_TIMEOUT_BASE=999\n")
    monkeypatch.setenv("AICP_TIMEOUT_BASE", "10")
    assert resolve().values["AICP_TIMEOUT_BASE"] == "10"


def test_hardcoded_default_applies_when_neither_sets_it(cfg):
    cfg("# an .aicprc that mentions no AICP_TIMEOUT_* knob at all\n")
    assert "AICP_TIMEOUT_BASE" not in resolve().values


def test_config_path_defaults_to_home_aicprc(monkeypatch):
    monkeypatch.delenv("AICP_CONFIG", raising=False)
    assert config_path() == Path(os.path.expanduser("~")) / ".aicprc"


# ── shape of a line: comments, blanks, malformed, non-AICP_ keys ─────────────


def test_comments_blank_and_malformed_lines_are_skipped(cfg):
    path = cfg(
        "# a comment line\n"
        "\n"
        "\n"
        "not a valid line, no equals sign at all\n"
        "=no key before the equals sign\n"
        "AICP_TIMEOUT_BASE=10\n"
    )
    assert load_config(path) == {"AICP_TIMEOUT_BASE": "10"}
    assert resolve().values["AICP_TIMEOUT_BASE"] == "10"


def test_non_aicp_key_named_path_is_ignored_and_a_later_key_still_works(cfg):
    before = os.environ["PATH"]
    cfg("PATH=/nonexistent-evil-path\nNOT_AICP_TIMEOUT_BASE=999\nAICP_TIMEOUT_BASE=10\n")
    values = resolve().values
    assert "PATH" not in values
    assert "NOT_AICP_TIMEOUT_BASE" not in values
    assert values["AICP_TIMEOUT_BASE"] == "10"
    assert os.environ["PATH"] == before


def test_lowercase_and_mixed_case_keys_are_refused(cfg):
    path = cfg("aicp_timeout_base=10\nAicp_Timeout_Base=10\n")
    assert load_config(path) == {}


def test_whitespace_and_quoted_values_are_tolerated(cfg):
    cfg('  AICP_TIMEOUT_BASE = "10"\n\tAICP_TZ=\'Etc/UTC\'\n')
    values = resolve().values
    assert values["AICP_TIMEOUT_BASE"] == "10"
    assert values["AICP_TZ"] == "Etc/UTC"


# ── SECURITY: a value is inert text, never code and never a path to code ─────


def test_command_substitution_is_never_executed(cfg, tmp_path):
    """The headline invariant: parsed line-by-line, never sourced or eval'd."""
    marker = tmp_path / "cmdsub.marker"
    cfg(f"AICP_TIMEOUT_BASE=$(touch {marker})\nAICP_TZ=Etc/UTC\n")

    settings = resolve()

    assert not marker.exists(), "a .aicprc command substitution was EXECUTED"
    assert "AICP_TIMEOUT_BASE" not in settings.values
    assert settings.values["AICP_TZ"] == "Etc/UTC", "one bad line must not eat the file"


def test_backtick_and_metacharacter_values_are_dropped(cfg):
    path = cfg(
        "AICP_TIMEOUT_BASE=`id`\n"
        "AICP_TIMEOUT_MAX=1;rm -rf /\n"
        "AICP_TIMEOUT_PER_FILE=5|tee\n"
        "AICP_TZ=Etc/UTC\n"
    )
    assert load_config(path) == {"AICP_TZ": "Etc/UTC"}


def test_arithmetic_assignment_value_never_reaches_path(cfg):
    """``AICP_TIMEOUT_BASE=PATH=0`` is zsh arithmetic assignment in the
    original; ``=`` is off the value allowlist so the whole line is dropped,
    and ``$PATH`` is provably untouched."""
    before = os.environ["PATH"]
    cfg("AICP_TIMEOUT_BASE=PATH=0\nAICP_TZ=Etc/UTC\n")

    settings = resolve()

    assert os.environ["PATH"] == before
    assert "AICP_TIMEOUT_BASE" not in settings.values
    assert settings.values["AICP_TZ"] == "Etc/UTC"


# ── SECURITY: the exec-path / path-mutation denylist ─────────────────────────


def test_denylist_names_both_environment_only_knobs():
    assert set(DENYLIST) == {"AICP_TG_SEND", "AICP_TIMING_LOG"}


@pytest.mark.parametrize("key", ["AICP_TG_SEND", "AICP_TIMING_LOG"])
def test_denylisted_key_from_config_is_refused_and_announced(
    cfg, marker_payload, capsys, key
):
    script, marker = marker_payload(key.lower())
    path = cfg(f"{key}={script}\n")

    settings = resolve()

    assert key not in load_config(path)
    assert settings.values.get(key) != str(script), "a denied .aicprc value WON"
    assert not marker.exists(), f"a .aicprc {key} payload was EXECUTED"
    assert key in capsys.readouterr().err


def test_denylisted_config_path_is_never_created(cfg, tmp_path, capsys):
    sentinel_dir = tmp_path / "sentinel"
    cfg(f"AICP_TIMING_LOG={sentinel_dir}/created/by/config/PWNED.txt\n")

    resolve()

    assert not sentinel_dir.exists(), "a .aicprc path was materialised"
    assert "AICP_TIMING_LOG" in capsys.readouterr().err


@pytest.mark.parametrize("key", ["AICP_TG_SEND", "AICP_TIMING_LOG"])
def test_denylisted_key_from_the_real_environment_still_works(cfg, monkeypatch, key):
    """The env-var escape hatch is the one legitimate, user-controlled way to
    set these — only the FILE is refused."""
    cfg(f"{key}=/config/supplied/path\n")
    monkeypatch.setenv(key, "/env/supplied/path")
    assert resolve().values[key] == "/env/supplied/path"


# ── SECURITY: AICP_TIMEOUT_BIN is resolved from PATH, never from config ──────


def test_timeout_bin_is_never_taken_from_config(cfg, capsys):
    path = cfg("AICP_TIMEOUT_BIN=/decoy/timeout\n")
    assert "AICP_TIMEOUT_BIN" not in load_config(path)
    assert "AICP_TIMEOUT_BIN" not in resolve().values
    assert "AICP_TIMEOUT_BIN" in capsys.readouterr().err


def test_timeout_bin_is_not_taken_from_the_environment_either(monkeypatch):
    """Unlike the denylisted knobs, this one has no env escape hatch — the
    zsh original clobbers it unconditionally, from both sources."""
    monkeypatch.setenv("AICP_TIMEOUT_BIN", "/decoy/timeout")
    assert "AICP_TIMEOUT_BIN" not in resolve().values


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stub")
def test_path_timeout_binary_is_invoked_and_the_config_decoy_is_not(
    cfg, marker_payload, tmp_path, monkeypatch
):
    real_dir = tmp_path / "real-bin"
    real_dir.mkdir()
    real_marker = tmp_path / "real.marker"
    real = real_dir / "timeout"
    real.write_text(f'#!/bin/sh\ntouch "{real_marker}"\n', encoding="utf-8")
    real.chmod(real.stat().st_mode | stat.S_IEXEC)
    decoy, decoy_marker = marker_payload("timeoutbin-decoy")

    cfg(f"AICP_TIMEOUT_BIN={decoy}\n")
    monkeypatch.setenv("PATH", f"{real_dir}{os.pathsep}{os.environ['PATH']}")

    resolved = timeout_bin()
    assert resolved is not None
    assert Path(resolved) == real
    subprocess.run([resolved], check=True)

    assert real_marker.exists(), "the PATH-detected timeout binary was not the one used"
    assert not decoy_marker.exists(), "a .aicprc AICP_TIMEOUT_BIN decoy was INVOKED"


# ── validation: announce and fall back, never abort ──────────────────────────


@pytest.mark.parametrize("value", ["", "2", "false", "no", "yes", "Asia/Taipei"])
def test_invalid_booleans_announce_and_fall_back_to_on(cfg, capsys, value):
    cfg(f"AICP_DO_COMMIT={value}\nAICP_DO_PUSH={value}\n")

    settings = resolve()

    assert settings.do_commit is True
    assert settings.do_push is True
    err = capsys.readouterr().err
    assert "AICP_DO_COMMIT" in err
    assert "AICP_DO_PUSH" in err


@pytest.mark.parametrize(("value", "expected"), [("0", False), ("1", True)])
def test_valid_booleans_are_honoured(cfg, value, expected):
    cfg(f"AICP_DO_COMMIT={value}\nAICP_DO_PUSH={value}\n")
    settings = resolve()
    assert settings.do_commit is expected
    assert settings.do_push is expected


def test_invalid_language_announces_and_falls_back_to_en(cfg, capsys):
    cfg("AICP_LANG=fr\n")
    assert resolve().lang == "en"
    assert "AICP_LANG" in capsys.readouterr().err


def test_valid_language_is_honoured(cfg):
    cfg("AICP_LANG=zh-TW\n")
    assert resolve().lang == "zh-TW"


def test_timezone_in_path_form_is_rejected(cfg, capsys):
    """``:/etc/passwd`` is the form that would make a TZ value name a file."""
    cfg("AICP_TZ=:/etc/passwd\n")
    assert resolve().tz == "Asia/Taipei"
    assert "AICP_TZ" in capsys.readouterr().err


def test_ordinary_iana_zone_is_accepted(cfg, capsys):
    cfg("AICP_TZ=Etc/UTC\n")
    assert resolve().tz == "Etc/UTC"
    assert "AICP_TZ" not in capsys.readouterr().err


def test_an_invalid_value_never_aborts_the_load(cfg):
    """Every validation failure is a fallback, not an exception — a bad file
    must never stop the run it is only supposed to configure."""
    cfg("AICP_DO_COMMIT=maybe\nAICP_LANG=fr\nAICP_TZ=:/etc/passwd\nAICP_CLI_ORDER=nope\n")
    settings = resolve()
    assert (settings.do_commit, settings.lang, settings.tz) == (True, "en", "Asia/Taipei")
    assert tuple(settings.cli_chain) == ROSTER_NAMES


# ── the resolved chain: the Sequence[str] T2 and T4 consume ──────────────────


def test_chain_defaults_to_the_roster_order():
    assert tuple(resolve_cli_chain(None)) == ROSTER_NAMES


def test_chain_accepts_a_prefix_and_appends_the_rest_in_roster_order():
    chain = resolve_cli_chain("vibe codex")
    assert tuple(chain) == ("vibe", "codex", "copilot", "agy", "claude")


def test_chain_is_a_plain_sequence_of_str():
    chain = resolve_cli_chain("vibe")
    assert all(isinstance(name, str) for name in chain)
    assert set(chain) == set(ROSTER_NAMES)


@pytest.mark.parametrize("order", ["nosuchcli", "copilot nosuchcli", "copilot copilot"])
def test_unknown_or_repeated_cli_name_is_refused_with_a_notice(capsys, order):
    assert tuple(resolve_cli_chain(order)) == ROSTER_NAMES
    assert "AICP_CLI_ORDER" in capsys.readouterr().err


@pytest.mark.parametrize(
    "order",
    ["/bin/sh", "../../bin/sh", "copilot /bin/sh", "copilot;id", "$(id)", "copilot\ttouch"],
)
def test_the_chain_can_only_ever_contain_roster_names(cfg, order):
    """SECURITY: the chain becomes ``argv[0]`` in T2's runner, so a config
    file must never be able to put an arbitrary binary into it. Whatever the
    file says, every element is a name that came from ROSTER."""
    cfg(f"AICP_CLI_ORDER={order}\n")
    assert set(resolve().cli_chain) <= set(ROSTER_NAMES)


def test_chain_comes_from_the_config_file(cfg):
    cfg("AICP_CLI_ORDER=vibe codex\n")
    assert tuple(resolve().cli_chain) == ("vibe", "codex", "copilot", "agy", "claude")


def test_env_cli_order_beats_the_config_file(cfg, monkeypatch):
    cfg("AICP_CLI_ORDER=vibe codex\n")
    monkeypatch.setenv("AICP_CLI_ORDER", "claude")
    assert next(iter(resolve().cli_chain)) == "claude"


# ── persist_key: rewrite one line, keep every other, atomically ──────────────


def test_persist_key_rewrites_in_place_and_preserves_unrelated_lines(tmp_path):
    path = tmp_path / "keep.aicprc"
    path.write_text(
        "# a hand-written comment\n"
        "AICP_CLI_ORDER=codex copilot agy claude vibe\n"
        "AICP_TZ=Etc/UTC\n"
        "AICP_DO_COMMIT=1\n",
        encoding="utf-8",
    )

    assert persist_key("AICP_DO_COMMIT", "0", path) is True

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "AICP_DO_COMMIT=0" in lines
    assert "AICP_DO_COMMIT=1" not in lines
    assert "# a hand-written comment" in lines
    assert "AICP_CLI_ORDER=codex copilot agy claude vibe" in lines
    assert "AICP_TZ=Etc/UTC" in lines


def test_persist_key_appends_when_the_key_is_absent(tmp_path):
    path = tmp_path / "append.aicprc"
    path.write_text("AICP_TZ=Etc/UTC\n", encoding="utf-8")
    persist_key("AICP_DO_PUSH", "0", path)
    assert path.read_text(encoding="utf-8").splitlines() == [
        "AICP_TZ=Etc/UTC",
        "AICP_DO_PUSH=0",
    ]


def test_persist_key_creates_the_file_when_missing(tmp_path):
    path = tmp_path / "nested" / "new.aicprc"
    persist_key("AICP_LANG", "zh-TW", path)
    assert path.read_text(encoding="utf-8") == "AICP_LANG=zh-TW\n"


def test_persist_key_leaves_no_temp_file_behind(tmp_path):
    holder = tmp_path / "cfgdir"
    holder.mkdir()
    path = holder / "atomic.aicprc"
    persist_key("AICP_LANG", "en", path)
    persist_key("AICP_LANG", "zh-TW", path)
    assert [p.name for p in holder.iterdir()] == ["atomic.aicprc"]


def test_persist_key_round_trips_through_the_loader(tmp_path, monkeypatch):
    path = tmp_path / "roundtrip.aicprc"
    monkeypatch.setenv("AICP_CONFIG", str(path))
    persist_key("AICP_DO_PUSH", "0", path)
    persist_key("AICP_CLI_ORDER", "vibe codex", path)
    settings = resolve()
    assert settings.do_push is False
    assert tuple(settings.cli_chain)[:2] == ("vibe", "codex")


def test_persist_key_reports_failure_instead_of_raising(tmp_path):
    """A write that cannot happen is a False, announced by the caller — the
    menu must keep running rather than die mid-repaint."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    assert persist_key("AICP_LANG", "en", blocked) is False


def test_load_config_reads_the_file_only(cfg, monkeypatch):
    """``load_config`` is the file half; the env half is ``resolve``'s job."""
    path = cfg("AICP_TIMEOUT_BASE=10\n")
    monkeypatch.setenv("AICP_TIMEOUT_BASE", "999")
    assert load_config(path) == {"AICP_TIMEOUT_BASE": "10"}
