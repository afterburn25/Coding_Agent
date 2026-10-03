"""Offline US postal lookup — states, cities, ZIPs.

``PostalProvider`` is the replaceable seam: ``BundledPostalProvider``
reads the packaged ``us_postal.json`` (state → city list, or city →
[zip, ...]) so onboarding works with no network. A future geocoder
provider implements the same three methods and can be swapped in via
``set_provider``.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Protocol

_DATA_PATH = Path(__file__).parent / "us_postal.json"


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(t.split()).casefold()


class PostalProvider(Protocol):
    def states(self) -> list[dict]: ...
    def cities(self, state: str) -> list[dict]: ...
    def zips(self, state: str, city: str) -> list[str]: ...


class BundledPostalProvider:
    """Local dataset — each state maps to either a list of city names or
    a dict of city → [ZIPs]. Cities with no bundled ZIPs return an empty
    list from ``zips()`` (the UI falls back to manual ZIP entry, which
    is still 5-digit validated — never guessed)."""

    def __init__(self, path: Path = _DATA_PATH) -> None:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except Exception:
            self._data = {}

    def states(self) -> list[dict]:
        return [{"code": code, "name": info.get("name", code)}
                for code, info in sorted(self._data.items())]

    def _state(self, state: str) -> dict:
        return self._data.get(str(state or "").strip().upper()) or {}

    def _city_map(self, state: str) -> dict:
        """Normalize both dataset shapes to city → [zips]."""
        cities = self._state(state).get("cities", {})
        if isinstance(cities, dict):
            return cities
        if isinstance(cities, list):
            return {str(c): [] for c in cities}
        return {}

    def cities(self, state: str) -> list[dict]:
        return [{"name": c, "zip_count": len(z)}
                for c, z in sorted(self._city_map(state).items())]

    def zips(self, state: str, city: str) -> list[str]:
        cities = self._city_map(state)
        if city in cities:
            return list(cities[city])
        # Normalized fallback — display/case differences shouldn't miss.
        key = _norm(city)
        for name, zips in cities.items():
            if _norm(name) == key:
                return list(zips)
        return []


_provider: PostalProvider = BundledPostalProvider()


def provider() -> PostalProvider:
    return _provider


def set_provider(p: PostalProvider) -> None:
    global _provider
    _provider = p
