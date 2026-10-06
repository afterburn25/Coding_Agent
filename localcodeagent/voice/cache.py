"""Bounded LRU cache for synthesized utterances (WAV files on disk).

Key = sha256 of normalized text + engine version + base voice + preset JSON
hash + speed — replaying a response hits cache instead of resynthesizing.
Disk usage is bounded; least-recently-used files evict first.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from ..fsutil import atomic_write_bytes


class AudioCache:
    def __init__(self, cache_dir: Path, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    @staticmethod
    def key(text: str, engine: str, engine_version: str, voice: str,
            preset_hash: str, speed: float) -> str:
        norm = " ".join(text.split()).strip().lower()
        h = hashlib.sha256(
            f"{engine}|{engine_version}|{voice}|{preset_hash}|{speed:.3f}|{norm}"
            .encode("utf-8"))
        return h.hexdigest()[:32]

    @staticmethod
    def preset_hash(preset_json: str) -> str:
        return hashlib.sha256(preset_json.encode("utf-8")).hexdigest()[:16]

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.wav"

    def get(self, key: str) -> Path | None:
        p = self._path(key)
        if p.exists():
            p.touch()
            return p
        return None

    def put(self, key: str, wav: bytes) -> Path:
        p = self._path(key)
        atomic_write_bytes(p, wav)
        self._evict()
        return p

    def _evict(self) -> None:
        files = sorted(self.dir.glob("*.wav"),
                       key=lambda f: f.stat().st_mtime if f.exists() else 0)
        total = sum(f.stat().st_size for f in files if f.exists())
        for f in files:
            if total <= self.max_bytes:
                break
            try:
                total -= f.stat().st_size
                f.unlink()
            except OSError:
                pass

    def stats(self) -> dict:
        # stat() each entry lazily with a race guard — an eviction can
        # unlink a wav between the glob and the stat; FileNotFoundError
        # used to crash the whole /status request.
        entries = 0
        total = 0
        for f in self.dir.glob("*.wav"):
            try:
                total += f.stat().st_size
                entries += 1
            except OSError:
                continue
        return {
            "entries": entries,
            "bytes": total,
            "max_bytes": self.max_bytes,
            "dir": str(self.dir),
        }
