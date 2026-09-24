"""The "Import/export settings" row of ``--config``.

A Tools row, not a subcommand, same as Skills/Agents/Doctor — see
``test_menu_skills.py``, whose fixtures and ``_row_number`` helper this file
mirrors. Driven through the NUMBERED fallback: the row's own two prompts
(export path, then import path) read from the same stdin the row-picker
does, so a full interaction is one multi-line ``keys`` string.

Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json

import pytest

from aicp.menu import ROWS, config_menu


def _row_number(msgid: str) -> int:
    return next(i for i, row in enumerate(ROWS, start=1) if row.label[0] == msgid)


SETTINGS_ROW = _row_number("config_settings_io")


@pytest.fixture
def menu(tmp_path, monkeypatch):
    """Factory: run the menu against a throwaway config with *keys* typed at
    its prompt. Returns ``(exit_code, output, config_path)``."""

    def _run(keys: str, *, initial: str | None = None):
        cfg = tmp_path / "menu.aicprc"
        if initial is not None:
            cfg.write_text(initial, encoding="utf-8")
        monkeypatch.setenv("AICP_CONFIG", str(cfg))
        out = io.StringIO()
        code = config_menu(stdin=io.StringIO(keys), stdout=out)
        return code, out.getvalue(), cfg

    return _run


def test_the_row_is_appended_to_the_row_model(menu):
    _, out, _ = menu("q\n")
    assert f"{SETTINGS_ROW}) Import/export settings" in out


def test_export_writes_the_current_settings_to_the_given_file(menu, tmp_path):
    dest = tmp_path / "out.json"
    code, out, cfg = menu(
        f"{SETTINGS_ROW}\n{dest}\nq\n",
        initial=json.dumps({"AICP_DO_COMMIT": "0", "AICP_LANG": "zh-TW"}),
    )
    assert code == 0
    assert "wrote" in out or "已寫入" in out
    assert json.loads(dest.read_text(encoding="utf-8")) == {
        "aicp_do_commit": "0",
        "aicp_lang": "zh-TW",
    }
    assert cfg.exists(), "export must not disturb the live config file"


def test_export_never_writes_a_denylisted_key(menu, tmp_path):
    dest = tmp_path / "out.json"
    menu(f"{SETTINGS_ROW}\n{dest}\nq\n", initial=json.dumps({"AICP_CONFIG": "/evil"}))
    assert "aicp_config" not in json.loads(dest.read_text(encoding="utf-8"))


def test_skipping_export_falls_through_to_the_import_prompt(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    code, out, cfg = menu(f"{SETTINGS_ROW}\n\n{source}\nq\n")
    assert code == 0
    assert "imported" in out or "已匯入" in out
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_do_push"] == "0"


def test_import_merges_into_the_existing_file_rather_than_replacing_it(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    _, _out, cfg = menu(
        f"{SETTINGS_ROW}\n\n{source}\nq\n",
        initial=json.dumps({"AICP_TZ": "Etc/UTC"}),
    )
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["aicp_do_push"] == "0"
    assert data["aicp_tz"] == "Etc/UTC"


def test_import_reports_skipped_keys_without_writing_them(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_CONFIG": "/evil", "AICP_LANG": "en"}), encoding="utf-8")
    _, out, cfg = menu(f"{SETTINGS_ROW}\n\n{source}\nq\n")
    assert "AICP_CONFIG" in out
    assert "aicp_config" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_lang"] == "en"


def test_skipping_both_prompts_writes_nothing(menu):
    code, _out, cfg = menu(f"{SETTINGS_ROW}\n\n\nq\n")
    assert code == 0
    assert not cfg.exists()


def test_import_reports_when_the_source_has_nothing_importable(menu, tmp_path):
    missing = tmp_path / "missing.json"
    _, out, cfg = menu(f"{SETTINGS_ROW}\n\n{missing}\nq\n")
    assert "nothing importable" in out or "沒有可匯入" in out
    assert not cfg.exists()


def test_export_reports_failure_instead_of_writing(monkeypatch, tmp_path):
    """Same posture as ``persist_key``: a write that cannot happen is a
    reported failure, and the menu keeps running rather than crashing."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
    out = io.StringIO()
    code = config_menu(stdin=io.StringIO(f"{SETTINGS_ROW}\n{blocked}\nq\n"), stdout=out)
    assert code == 0
    assert "could not write" in out.getvalue() or "無法寫入" in out.getvalue()


def test_import_refreshes_the_toggle_rows_shown_by_the_menu(menu, tmp_path):
    """An imported value must repaint, not just persist — the same promise
    every other row's ``cycle`` already makes."""
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_COMMIT": "0"}), encoding="utf-8")
    _, out, _cfg = menu(f"{SETTINGS_ROW}\n\n{source}\nq\n")
    commit_lines = [ln for ln in out.splitlines() if "Run the /commit step" in ln]
    # Printed twice: once before the import (still On), once in the panel
    # the menu loop repaints right after — that second one must show Off.
    assert "Off" in commit_lines[-1]
