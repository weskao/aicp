"""PyPI update check. Stdlib only — copy this file into other projects.

    from update_check import check
    found = check(
        "my-dist",
        current="1.0.0",
        cache_path=Path.home() / ".myapp" / "update-check.json",
    )
    if found:
        print(f"{found.latest} is available (you have {found.current})")

Nothing here is allowed to raise to the caller: a missing cache, a bad
payload, or a dead network all become ``None``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_TIMEOUT", "DEFAULT_TTL", "UpdateAvailable", "check", "fetch_pypi"]

DEFAULT_TTL = 86_400
DEFAULT_TIMEOUT = 0.8


@dataclass(frozen=True)
class UpdateAvailable:
    current: str
    latest: str


def fetch_pypi(dist_name: str, timeout: float) -> str:
    """GET ``https://pypi.org/pypi/<dist>/json`` and return the raw body."""
    url = f"https://pypi.org/pypi/{dist_name}/json"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"{dist_name}-update-check",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def check(
    dist_name: str,
    current: str,
    *,
    cache_path: Path,
    ttl_seconds: int = DEFAULT_TTL,
    timeout: float = DEFAULT_TIMEOUT,
    now: float | None = None,
    fetch: Callable[[str, float], str] | None = None,
) -> UpdateAvailable | None:
    """Return an update when PyPI's latest is newer than *current*, else None."""
    current_parts = _numeric_tuple(current)
    if current_parts is None:
        return None
    stamp = _now(now)
    cached = _load_cache(cache_path)
    latest: str | None = None
    cached_latest = cached.get("latest")
    checked_at = cached.get("checked_at")
    fresh = (
        isinstance(cached_latest, str)
        and isinstance(checked_at, (int, float))
        and stamp - float(checked_at) < ttl_seconds
    )
    if fresh:
        latest = cached_latest
    else:
        try:
            body = (fetch or fetch_pypi)(dist_name, timeout)
            latest = _latest_from_body(body)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError):
            latest = cached_latest if isinstance(cached_latest, str) else None
        else:
            if latest is not None:
                _save_cache(cache_path, stamp, latest)
    if not isinstance(latest, str):
        return None
    latest_parts = _numeric_tuple(latest)
    if latest_parts is None or latest_parts <= current_parts:
        return None
    return UpdateAvailable(current=current, latest=latest)


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def _numeric_tuple(version: str) -> tuple[int, ...] | None:
    """Leading dotted integers. ``0.10.0`` > ``0.9.1``. Needs at least X.Y."""
    parts: list[int] = []
    for chunk in version.split("."):
        digits = ""
        for char in chunk:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    if len(parts) < 2:
        return None
    return tuple(parts)


def _latest_from_body(body: str) -> str | None:
    data = json.loads(body)
    if not isinstance(data, dict):
        return None
    info = data.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    if isinstance(version, str) and version:
        return version
    return None


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(path: Path, checked_at: float, latest: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": checked_at, "latest": latest}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return
