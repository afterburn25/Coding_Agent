"""Local embeddings for semantic question matching.

Default backend: ``hashed-ngram-v1`` — a deterministic feature-hashing
embedder (word unigrams + bigrams + character 4-grams into a fixed dense
vector, L2-normalized). It needs no model download, runs in microseconds on
CPU, and is stable across restarts, which makes it ideal for short-question
paraphrase matching. The backend is versioned per row so a future ONNX/neural
embedder can replace it and the index can be rebuilt selectively.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Iterable

from .normalization import expanded_tokens, normalize_question

EMBEDDER_ID = "hashed-ngram-v1"
EMBEDDER_DIM = 384

try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy optional
    _np = None


def _hash_index(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    # Signed hashing (Weinberger-style) reduces collision bias.
    return value % dim, 1.0 if (value >> 63) == 0 else -1.0


def _features(text: str) -> Iterable[str]:
    norm = normalize_question(text)
    words = expanded_tokens(text)
    for w in words:
        yield "w:" + w
    for a, b in zip(words, words[1:]):
        yield "b:" + a + " " + b


class HashedNgramEmbedder:
    """Deterministic local embedder — no model download, no GPU."""

    id = EMBEDDER_ID
    dim = EMBEDDER_DIM

    def embed(self, text: str) -> bytes:
        vec = [0.0] * self.dim
        counts: dict[int, float] = {}
        for feat in _features(text):
            idx, sign = _hash_index(feat, self.dim)
            weight = sign * (1.6 if feat.startswith("w:") else 1.2 if feat.startswith("b:") else 1.0)
            counts[idx] = counts.get(idx, 0.0) + weight
        norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
        for idx, v in counts.items():
            vec[idx] = v / norm
        return struct.pack(f"<{self.dim}f", *vec)

    def embed_batch(self, texts: list[str]) -> list[bytes]:
        return [self.embed(t) for t in texts]


def decode(blob: bytes | None, dim: int = EMBEDDER_DIM):
    """Decode a stored embedding. Returns a numpy array when available else a list."""
    if not blob:
        return None
    try:
        expected = dim * 4
        if len(blob) != expected:
            return None
        if _np is not None:
            return _np.frombuffer(blob, dtype="<f4").astype("float64")
        return list(struct.unpack(f"<{dim}f", blob))
    except Exception:
        return None


def cosine(a, b) -> float:
    """Cosine similarity for numpy arrays or plain lists."""
    if a is None or b is None:
        return 0.0
    if _np is not None and not isinstance(a, list) and not isinstance(b, list):
        denom = float(_np.linalg.norm(a) * _np.linalg.norm(b))
        if denom <= 0:
            return 0.0
        return float(_np.dot(a, b) / denom)
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(y * y for y in b))
    if da <= 0 or db <= 0:
        return 0.0
    return num / (da * db)


_default = HashedNgramEmbedder()


def embedder() -> HashedNgramEmbedder:
    return _default
