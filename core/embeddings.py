"""Local text embeddings for the Study Vault (Phase 9).

Pluggable so the vault stays free and offline-testable:
  * SentenceTransformerEmbedder — all-MiniLM-L6-v2 (best quality; used on the droplet
    when sentence-transformers is installed; downloads a small model once, CPU-fine);
  * HashingEmbedder — a dependency-free, deterministic hashed bag-of-words fallback
    (no torch, no network) used when the library is absent and in tests.
config.yaml `vault.embedder: auto|sentence-transformers|hashing` picks the strategy.
Vectors are stored as packed float32 BLOBs (see core.memory); cosine runs in Python.
"""

from __future__ import annotations

import array
import hashlib
import math
import re
from typing import Protocol

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.embeddings")

_WORD_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic hashed bag-of-words with sublinear TF + L2 norm. No dependencies."""

    name = "hashing"

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _WORD_RE.findall((text or "").lower()):
            # stable hash (NOT built-in hash(), which is per-process salted) so embeddings
            # persisted to SQLite still match queries after a restart.
            digest = int.from_bytes(hashlib.md5(tok.encode("utf-8")).digest()[:8], "little")
            h = digest % self.dim
            sign = 1.0 if (digest // self.dim) % 2 == 0 else -1.0
            vec[h] += sign
        # sublinear scaling + L2 normalize
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """all-MiniLM-L6-v2 via sentence-transformers (lazy — imported/loaded on first use)."""

    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)
        self.dim = self._model.get_sentence_embedding_dimension()

    def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.encode(texts, normalize_embeddings=True)]


def get_embedder(strategy: str | None = None) -> Embedder:
    """Return an embedder per config (auto tries sentence-transformers, falls back to hashing)."""
    strategy = strategy or get_settings().get("vault", "embedder", default="auto")
    if strategy in ("sentence-transformers", "auto"):
        try:
            emb = SentenceTransformerEmbedder()
            log.info("Vault using sentence-transformers embeddings (dim=%d)", emb.dim)
            return emb
        except Exception as exc:  # noqa: BLE001 - lib missing or model load failed
            if strategy == "sentence-transformers":
                log.warning("sentence-transformers requested but unavailable (%s) — using hashing", exc)
            else:
                log.info("sentence-transformers not available — using dependency-free hashing embedder")
    return HashingEmbedder()


# --------------------------------------------------------------- vector (de)serialization + cosine
def pack_vector(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Assumes similar dims; returns 0 on mismatch/degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
