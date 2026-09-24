"""PyPI update hint — stdlib checker other projects can copy.

Never hits the network: every test injects ``fetch``. A production change
that always returns None, that treats 0.10.0 as not newer than 0.9.1, or
that fetches on every call despite a fresh cache, is what these catch.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from aicp import update_check


def _pypi(version: str) -> str:
    return json.dumps({"info": {"version": version}})


def _check(
    tmp_path: Path,
    *,
    current: str = "0.9.1",
    latest: str = "0.10.0",
    now: float = 1_000_000.0,
    ttl: int = 86_400,
    fetch=None,
    calls: list | None = None,
):
    cache = tmp_path / "update-check.json"

    def _fetch(dist: str, timeout: float) -> str:
        if calls is not None:
            calls.append((dist, timeout))
        if fetch is not None:
            return fetch(dist, timeout)
        return _pypi(latest)

    return update_check.check(
        "aicp-cli",
        current,
        cache_path=cache,
        ttl_seconds=ttl,
        timeout=0.8,
        now=now,
        fetch=_fetch,
    )


def test_a_newer_pypi_release_is_reported(tmp_path):
    found = _check(tmp_path, current="0.9.1", latest="0.10.0")
    assert found is not None
    assert found.current == "0.9.1"
    assert found.latest == "0.10.0"


def test_matching_versions_stay_silent(tmp_path):
    assert _check(tmp_path, current="0.9.1", latest="0.9.1") is None


def test_an_older_pypi_release_stays_silent(tmp_path):
    assert _check(tmp_path, current="0.9.1", latest="0.8.0") is None


def test_a_fresh_cache_does_not_refetch(tmp_path):
    calls: list = []
    cache = tmp_path / "update-check.json"
    cache.write_text(
        json.dumps({"checked_at": 1_000_000.0, "latest": "0.10.0"}),
        encoding="utf-8",
    )
    found = update_check.check(
        "aicp-cli",
        "0.9.1",
        cache_path=cache,
        ttl_seconds=86_400,
        now=1_000_000.0 + 60,
        fetch=lambda dist, timeout: calls.append((dist, timeout)) or _pypi("9.9.9"),
    )
    assert found is not None
    assert found.latest == "0.10.0"
    assert calls == []


def test_a_stale_cache_refetches(tmp_path):
    calls: list = []
    cache = tmp_path / "update-check.json"
    cache.write_text(
        json.dumps({"checked_at": 1.0, "latest": "0.9.2"}),
        encoding="utf-8",
    )
    found = update_check.check(
        "aicp-cli",
        "0.9.1",
        cache_path=cache,
        ttl_seconds=86_400,
        now=1.0 + 86_401,
        fetch=lambda dist, timeout: calls.append((dist, timeout)) or _pypi("0.10.0"),
    )
    assert found is not None
    assert found.latest == "0.10.0"
    assert calls == [("aicp-cli", 0.8)]


def test_a_failed_fetch_stays_silent(tmp_path):
    def boom(_dist: str, _timeout: float) -> str:
        raise OSError("offline")

    assert _check(tmp_path, fetch=boom) is None


def test_malformed_pypi_json_stays_silent(tmp_path):
    assert _check(tmp_path, fetch=lambda *_a: "not-json") is None


def test_an_unparseable_current_version_stays_silent(tmp_path):
    assert _check(tmp_path, current="0+unknown", latest="0.10.0") is None


def test_a_failed_fetch_reuses_stale_latest(tmp_path):
    cache = tmp_path / "update-check.json"
    cache.write_text(
        json.dumps({"checked_at": 1.0, "latest": "0.10.0"}),
        encoding="utf-8",
    )

    def boom(_dist: str, _timeout: float) -> str:
        raise OSError("offline")

    found = update_check.check(
        "aicp-cli",
        "0.9.1",
        cache_path=cache,
        ttl_seconds=86_400,
        now=1.0 + 86_401,
        fetch=boom,
    )
    assert found is not None
    assert found.latest == "0.10.0"


def test_minor_version_ten_is_newer_than_nine(tmp_path):
    """0.10.0 must not sort as older than 0.9.1 (string compare would)."""
    found = _check(tmp_path, current="0.9.1", latest="0.10.0")
    assert found is not None
    assert found.latest == "0.10.0"


def test_the_checker_imports_only_the_stdlib():
    """Other projects copy this file; it must not import aicp."""
    tree = ast.parse(Path(update_check.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "aicp" not in imported
    stdlib = {
        "__future__",
        "json",
        "threading",
        "time",
        "urllib",
        "collections",
        "dataclasses",
        "pathlib",
    }
    assert imported <= stdlib


def test_start_and_collect_finds_an_update(tmp_path):
    """The background variant of check() must agree with the synchronous one."""
    started = update_check.start(
        "aicp-cli",
        "0.9.1",
        cache_path=tmp_path / "update-check.json",
        fetch=lambda *_a: _pypi("0.10.0"),
    )
    found = update_check.collect(started)
    assert found is not None
    assert found.latest == "0.10.0"


def test_start_and_collect_stays_silent_when_current(tmp_path):
    started = update_check.start(
        "aicp-cli",
        "0.9.1",
        cache_path=tmp_path / "update-check.json",
        fetch=lambda *_a: _pypi("0.9.1"),
    )
    assert update_check.collect(started) is None


def test_start_overlaps_with_the_callers_own_work(tmp_path):
    """The point of start()/collect(): the fetch runs while the caller does other work."""
    import threading
    import time as time_mod

    release_fetch = threading.Event()

    def slow_fetch(_dist: str, _timeout: float) -> str:
        release_fetch.wait(timeout=1)
        return _pypi("0.10.0")

    started = update_check.start(
        "aicp-cli", "0.9.1", cache_path=tmp_path / "update-check.json", fetch=slow_fetch
    )
    # The caller's own work happens here, concurrently with the fetch.
    time_mod.sleep(0.05)
    assert started.thread.is_alive()
    release_fetch.set()
    found = update_check.collect(started)
    assert found is not None
    assert found.latest == "0.10.0"


def test_collect_with_no_started_check_is_silent():
    assert update_check.collect(None) is None


def test_default_ttl_is_short_enough_for_same_day_releases():
    # Several releases can land in one day: the next one is seen soon, not tomorrow.
    assert update_check.DEFAULT_TTL <= 3600
