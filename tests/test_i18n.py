"""The zh-TW catalogue must stay complete — checked against the source, not a list.

A translation catalogue rots the moment someone adds a string and forgets the
other language. The only durable guard is to re-derive the set of message ids
from ``src/`` on every run, so the test grows itself.

Collection is done with :mod:`ast`, never a regex: most of this project's
``t()`` calls span several lines, several are nested inside an f-string, and
``menu.ROWS`` keeps its ids in data rather than at a call site. Two passes:

* **call sites** — the msgid argument of ``t(msgid, english, ...)``,
  ``_t(lang, msgid, english, ...)`` and ``menu._ask(state, stdin, out, msgid,
  english, ...)``, when it is a literal;
* **tables** — string literals shaped like a msgid (``lower_snake`` with at
  least one ``_``) inside a tuple literal, or inside a dict literal that is not
  keyed by plain strings. That is exactly the shape of ``Row.label``/``help``/
  ``group`` and of the ``{state: msgid}`` maps in ``menu``; it deliberately
  skips string-keyed maps such as ``cli._SUB_FLAGS``, whose values are argparse
  destinations, and the catalogue itself.

The second pass is a heuristic, so it is pinned by
:func:`test_the_catalogue_has_no_entries_that_no_call_site_uses`: if the sweep
ever stopped seeing a table, that table's entries would show up as orphans and
fail. Missing coverage and dead weight are caught by the same pair of tests.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from aicp.i18n import CATALOG, LANGUAGE, t

SRC = Path(__file__).resolve().parents[1] / "src"

#: ``lower_snake`` with at least one underscore — an English default ("repo",
#: "Off", "New commits") never matches, so the table sweep cannot mistake one
#: for an id.
MSGID = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")

#: Which positional argument carries the msgid, per translating function.
MSGID_ARG = {"t": 0, "_t": 1, "_ask": 3}


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, for resolving f-strings."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name, value = node.targets[0], node.value
            if isinstance(name, ast.Name) and _literal(value) is not None:
                found[name.id] = value.value
    return found


def _literal(node: ast.expr | None, consts: dict[str, str] | None = None) -> str | None:
    """The string *node* spells out, or None if it is not fully static.

    f-strings count when every interpolation is a module-level string constant
    — ``f"{_WARN} %s missing"`` is as fixed as its quoted form, and leaving it
    out would quietly exempt those call sites from the ``%s`` check below.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr) and consts is not None:
        parts: list[str] = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            elif (
                isinstance(piece, ast.FormattedValue)
                and isinstance(piece.value, ast.Name)
                and piece.value.id in consts
            ):
                parts.append(consts[piece.value.id])
            else:
                return None
        return "".join(parts)
    return None


def _collect() -> tuple[dict[str, set[str]], dict[str, str]]:
    """``{msgid: {english default, ...}}`` and ``{msgid: "file:line"}``.

    A msgid reached only through a table has no English default to pair with
    (the two dicts in ``menu._skills_health`` are parallel, not adjacent), so
    its entry is an empty set: covered by the completeness test, skipped by the
    ones that need both sides.
    """
    englishes: dict[str, set[str]] = defaultdict(set)
    origin: dict[str, str] = {}

    def note(msgid: str, english: str | None, path: Path, lineno: int) -> None:
        origin.setdefault(msgid, f"{path.relative_to(SRC.parent)}:{lineno}")
        bucket = englishes[msgid]  # registers the id even with no English
        if english is not None:
            bucket.add(english)

    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        consts = _module_constants(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                i = MSGID_ARG.get(name)
                if i is not None and len(node.args) > i:
                    msgid = _literal(node.args[i])
                    if msgid is not None:
                        english = (
                            _literal(node.args[i + 1], consts) if len(node.args) > i + 1 else None
                        )
                        note(msgid, english, path, node.lineno)
                continue
            if isinstance(node, ast.Tuple):
                elements = node.elts
            elif isinstance(node, ast.Dict) and not all(
                _literal(key) is not None for key in node.keys if key is not None
            ):
                elements = node.values
            else:
                continue
            for i, element in enumerate(elements):
                msgid = _literal(element)
                if msgid is None or not MSGID.match(msgid):
                    continue
                english = _literal(elements[i + 1]) if i + 1 < len(elements) else None
                if english is not None and MSGID.match(english):
                    english = None  # the next entry of a {state: msgid} map
                note(msgid, english, path, element.lineno)

    return dict(englishes), origin


MSGIDS, ORIGIN = _collect()


def test_the_collector_sees_the_call_sites_it_claims_to():
    """A collector that silently found nothing would make every other test in
    this file pass while proving nothing."""
    assert len(MSGIDS) > 100, MSGIDS
    # one per collection pass: a plain call, a multi-line call, a ROWS tuple
    assert "result_title" in MSGIDS
    assert "skills_keep_note" in MSGIDS
    assert "config_help_doctor" in MSGIDS


def test_every_msgid_used_in_src_has_a_zh_tw_translation():
    missing = sorted(set(MSGIDS) - set(CATALOG))
    assert not missing, "no zh-TW entry in aicp.i18n.CATALOG for: " + ", ".join(
        f"{msgid} ({ORIGIN[msgid]})" for msgid in missing
    )


def test_the_catalogue_has_no_entries_that_no_call_site_uses():
    orphans = sorted(set(CATALOG) - set(MSGIDS))
    assert not orphans, (
        "translated but never used (delete them, or the collector above missed "
        "a call site): " + ", ".join(orphans)
    )


def test_no_two_msgids_share_an_english_default():
    """Two ids for one string drift: the next person translates one of them."""
    ids_by_english: dict[str, set[str]] = defaultdict(set)
    for msgid, englishes in MSGIDS.items():
        for english in englishes:
            ids_by_english[english].add(msgid)
    shared = {
        english: sorted(ids) for english, ids in ids_by_english.items() if len(ids) > 1
    }
    assert not shared, f"one English default, several msgids — pick one id: {shared}"


def test_no_msgid_carries_two_different_english_defaults():
    """The catalogue can only hold one translation per id, so a second English
    default is silently mistranslated at one of the two call sites."""
    forked = {msgid: sorted(e) for msgid, e in MSGIDS.items() if len(e) > 1}
    assert not forked, f"same msgid, different English: {forked}"


#: The only msgids whose English default the collector cannot pair up: their id
#: and their text live in two parallel dicts in ``menu._skills_health``, so
#: nothing is adjacent to anything. Pinned rather than tolerated — a new
#: unpairable msgid means the ``%s`` test below stops covering it, and that has
#: to be a decision someone makes on purpose.
UNPAIRABLE = {"health_skill_missing", "health_skill_older", "health_skill_foreign"}


def test_only_the_known_msgids_lack_an_english_default_to_check_against():
    unpaired = {msgid for msgid, englishes in MSGIDS.items() if not englishes}
    assert unpaired == UNPAIRABLE, (
        "the %s check below silently skips these; pass the English default as a "
        f"plain literal, or add it to UNPAIRABLE on purpose: {sorted(unpaired - UNPAIRABLE)}"
    )


def test_every_translation_takes_the_same_arguments_as_its_english():
    """A ``%s`` gained or lost in translation is a crash at print time, in the
    one language whose speaker is least likely to be running the test suite."""
    wrong = {
        msgid: (english, CATALOG[msgid])
        for msgid, englishes in MSGIDS.items()
        for english in englishes
        if msgid in CATALOG and CATALOG[msgid].count("%s") != english.count("%s")
    }
    assert not wrong, f"%s count differs between English and zh-TW: {wrong}"


# ── the mechanism, end to end ────────────────────────────────────────────────


def test_t_returns_english_when_the_language_is_english():
    assert LANGUAGE == "en", "the suite runs unlocalised; see conftest"
    assert t("result_title", "RESULT") == "RESULT"
    assert t("no_such_msgid", "verbatim %") == "verbatim %"


def _help(lang: str) -> str:
    env = {**os.environ, "AICP_LANG": lang}
    out = subprocess.run(
        [sys.executable, "-c", "from aicp.cli import main; main(['--help'])"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return out.stdout


def test_help_is_actually_translated():
    english, chinese = _help("en"), _help("zh-TW")
    assert "--swap-ai" in english and "--swap-ai" in chinese, "flags stay as typed"
    assert english != chinese
    assert CATALOG["help_swap_ai"] in chinese


def test_an_unknown_language_warns_and_falls_back_to_english():
    env = {**os.environ, "AICP_LANG": "fr"}
    out = subprocess.run(
        [sys.executable, "-c", "import aicp.i18n as i;print(i.LANGUAGE)"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert out.stdout.strip() == "en"
    assert "AICP_LANG=fr" in out.stderr
