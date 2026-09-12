"""Message mechanism only — the catalogue itself is filled in by a later task.

Shape copied from ``~/scripts/bin/aicp``'s ``_aicp_t``/``_AICP_ZH`` (itself
copied from ai-accounts' ``i18n.py``): the English text stays at the call
site as ``t()``'s second argument (so the printed wording stays readable
where it's used, and English output never depends on a lookup that could
silently miss); only the zh-TW translation lives in a catalogue dict here. A
missing msgid, or one with no zh-TW entry yet, degrades to the English text
passed in — never a blank line, never a crash.

``AICP_LANG`` is resolved once, at import time (same posture as ai-accounts'
language resolution) — ``en`` or ``zh-TW`` only; anything else is a value the
writer got wrong, so it is announced to stderr and treated as ``en``, never
silently coerced or guessed at.
"""

from __future__ import annotations

import os
import sys

VALID_LANGUAGES = ("en", "zh-TW")
DEFAULT_LANGUAGE = "en"


def _resolve_language() -> str:
    raw = os.environ.get("AICP_LANG", DEFAULT_LANGUAGE)
    if raw not in VALID_LANGUAGES:
        print(
            f"aicp: ignoring AICP_LANG={raw} (expected en or zh-TW) — using en",
            file=sys.stderr,
        )
        return DEFAULT_LANGUAGE
    return raw


# Resolved once, at import — mirrors ai-accounts' i18n posture. A test that
# needs a different language must re-import this module (or call
# `_resolve_language()` directly) after patching the environment.
LANGUAGE = _resolve_language()

# Catalogue: msgid -> zh-TW translation. Deliberately near-empty — filling
# this in is a later task's job. House style (from ai-accounts / bin/aicp):
# fullwidth ：，。（）, a single half-width space around embedded Latin/number
# runs, product and command nouns left untranslated, the same emoji per
# status in both languages, "·" as separator, " — " to attach a trailing
# clause.
CATALOG: dict[str, str] = {}


def t(msgid: str, english: str, *args: object) -> str:
    """Look up *msgid*; zh-TW translation if resolved and present, else
    *english*. With no *args* the text is returned verbatim (never run
    through ``%``-formatting), so a literal ``%`` in a message can never be
    misread as a format specifier.
    """
    text = CATALOG.get(msgid, english) if LANGUAGE != "en" else english
    if args:
        return text % args
    return text
