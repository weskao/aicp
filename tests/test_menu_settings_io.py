"""The "Export settings" / "Import settings" rows of ``--config``.

A Tools row, not a subcommand, same as Skills/Agents/Doctor — see
``test_menu_skills.py``, whose fixtures and ``_row_number`` helper this file
mirrors. Driven through the NUMBERED fallback: each row's path prompt reads
from the same stdin the row-picker does, so a full interaction is one
multi-line ``keys`` string.

Local fixtures only — ``tests/conftest.py`` is never edited from here.
"""

from __future__ import annotations

import io
import json

import pytest

from aicp.menu import ROWS, config_menu


def _row_number(msgid: str) -> int:
    return next(i for i, row in enumerate(ROWS, start=1) if row.label[0] == msgid)


EXPORT_ROW = _row_number("config_settings_export")
IMPORT_ROW = _row_number("config_settings_import")


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


def test_export_and_import_are_separate_rows(menu):
    _, out, _ = menu("q\n")
    assert f"{EXPORT_ROW}) Export settings" in out
    assert f"{IMPORT_ROW}) Import settings" in out


def test_export_writes_the_current_settings_to_the_given_file(menu, tmp_path):
    dest = tmp_path / "out.json"
    code, out, cfg = menu(
        f"{EXPORT_ROW}\n{dest}\nq\n",
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
    menu(f"{EXPORT_ROW}\n{dest}\nq\n", initial=json.dumps({"AICP_CONFIG": "/evil"}))
    assert "aicp_config" not in json.loads(dest.read_text(encoding="utf-8"))


def test_import_loads_the_given_file(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    code, out, cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    assert code == 0
    assert "imported" in out or "已匯入" in out
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_do_push"] == "0"


def test_import_merges_into_the_existing_file_rather_than_replacing_it(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_PUSH": "0"}), encoding="utf-8")
    _, _out, cfg = menu(
        f"{IMPORT_ROW}\n{source}\nq\n",
        initial=json.dumps({"AICP_TZ": "Etc/UTC"}),
    )
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["aicp_do_push"] == "0"
    assert data["aicp_tz"] == "Etc/UTC"


def test_import_reports_skipped_keys_without_writing_them(menu, tmp_path):
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_CONFIG": "/evil", "AICP_LANG": "en"}), encoding="utf-8")
    _, out, cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    assert "AICP_CONFIG" in out
    assert "aicp_config" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert json.loads(cfg.read_text(encoding="utf-8"))["aicp_lang"] == "en"


@pytest.mark.parametrize("row", [EXPORT_ROW, IMPORT_ROW])
def test_cancelling_writes_nothing(menu, row):
    code, _out, cfg = menu(f"{row}\n\nq\n")
    assert code == 0
    assert not cfg.exists()


def test_import_reports_when_the_source_has_nothing_importable(menu, tmp_path):
    missing = tmp_path / "missing.json"
    _, out, cfg = menu(f"{IMPORT_ROW}\n{missing}\nq\n")
    assert "nothing importable" in out or "沒有可匯入" in out
    assert not cfg.exists()


def test_export_reports_failure_instead_of_writing(monkeypatch, tmp_path):
    """Same posture as ``persist_key``: a write that cannot happen is a
    reported failure, and the menu keeps running rather than crashing."""
    blocked = tmp_path / "a-file"
    blocked.write_text("", encoding="utf-8")
    monkeypatch.setenv("AICP_CONFIG", str(tmp_path / "menu.aicprc"))
    out = io.StringIO()
    code = config_menu(stdin=io.StringIO(f"{EXPORT_ROW}\n{blocked / 'out.json'}\nq\n"), stdout=out)
    assert code == 0
    assert "could not write" in out.getvalue() or "無法寫入" in out.getvalue()


def test_import_refreshes_the_toggle_rows_shown_by_the_menu(menu, tmp_path):
    """An imported value must repaint, not just persist — the same promise
    every other row's ``cycle`` already makes."""
    source = tmp_path / "in.json"
    source.write_text(json.dumps({"AICP_DO_COMMIT": "0"}), encoding="utf-8")
    _, out, _cfg = menu(f"{IMPORT_ROW}\n{source}\nq\n")
    commit_lines = [ln for ln in out.splitlines() if "Run the /commit step" in ln]
    # Printed twice: once before the import (still On), once in the panel
    # the menu loop repaints right after — that second one must show Off.
    assert "Off" in commit_lines[-1]


def test_export_to_a_folder_names_the_file_itself(menu, tmp_path):
    folder = tmp_path / "backup"
    folder.mkdir()
    menu(f"{EXPORT_ROW}\n{folder}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    (written,) = folder.glob("aicp-settings-*.json")
    assert json.loads(written.read_text(encoding="utf-8")) == {"aicp_lang": "en"}


def test_export_adds_json_to_a_bare_name(menu, tmp_path):
    menu(f"{EXPORT_ROW}\n{tmp_path / 'mine'}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    assert (tmp_path / "mine.json").exists()


@pytest.mark.parametrize("quote", ["'", '"'])
def test_quoted_paths_are_unquoted(menu, tmp_path, monkeypatch, quote):
    """Finder/Explorer "copy as path" wraps the path in quotes; kept literally,
    that wrote a relative ``"/Users/...`` tree into the working directory."""
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "with space"
    folder.mkdir()
    menu(f"{EXPORT_ROW}\n{quote}{folder}{quote}\nq\n", initial=json.dumps({"AICP_LANG": "en"}))
    assert list(folder.glob("aicp-settings-*.json"))
    assert not (tmp_path / quote).exists()
