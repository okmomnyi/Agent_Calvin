"""Semantic memory (Phase 33) — retrieve what's relevant instead of stuffing everything.

Calvin: "enhance the brain reasoning and performance ... set up vector databases instead of
stuffing it with too much in context."

He is describing a real and growing problem. Every CV tailor sent ALL 23 verified persona
facts — 4,725 characters, ~1,181 tokens — regardless of the job. A Node.js role received his
Docker facts, his Spotify listening habits and his collaborator list. That costs three ways:

  * **Reasoning.** Irrelevant context is noise. A model asked to weigh 23 facts, 20 of which
    do not apply, writes a blander CV than one handed the 6 that do.
  * **Latency and cost.** Those tokens are paid for on every call, and the write route was
    already timing out on big prompts.
  * **It gets worse.** Every fact added makes every prompt heavier. Left alone, the system
    degrades as it learns, which is precisely backwards.

So: embed each fact once, and at prompt time retrieve only what the question is about.

Storage is pgvector rather than a separate vector service. Postgres is already here, already
backed up, already the thing every process shares — and `<=>` with an HNSW index is a real
ANN search, not cosine-in-Python over every row. Adding Chroma or Qdrant would mean another
service to run, back up and reason about, to do something the database already does.

Degrades honestly: if pgvector is unavailable, `search()` falls back to keyword overlap and
says so, rather than silently returning nothing and leaving prompts empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from core.embeddings import get_embedder
from core.logging_setup import get_logger
from core.memory import Memory, get_memory

log = get_logger("core.semantic")

# Kinds of thing that can be recalled. Kept explicit so a caller cannot silently create a new
# namespace that nothing ever searches.
KIND_FACT = "fact"          # persona/CV facts
KIND_NOTE = "note"          # durable notes learned from conversation (Phase 34)
KIND_DOC = "doc"            # vault chunks
KINDS = (KIND_FACT, KIND_NOTE, KIND_DOC)

# Nearest-neighbour search always returns something, however unrelated. Without a floor, a
# Node.js job description still pulls in Calvin's Spotify genres at score 0.0 -- reinstating
# the noise this module exists to remove. Deliberately low: the hashing fallback embedder
# scores real matches around 0.3-0.4, so anything higher would silently starve the prompt.
MIN_RELEVANCE = 0.05


@dataclass
class Hit:
    ref: str
    kind: str
    text: str
    score: float
    meta: dict[str, Any]


class SemanticMemory:
    """Embed once, retrieve by meaning."""

    def __init__(self, memory: Memory | None = None, embedder: Any | None = None) -> None:
        self._mem = memory
        self._embedder = embedder
        self._vector_ok: bool | None = None

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = get_embedder("auto")
        return self._embedder

    # ------------------------------------------------------------- capability
    def vector_available(self) -> bool:
        """Whether pgvector is usable. Checked once; falls back to keywords if not."""
        if self._vector_ok is None:
            try:
                self.mem.execute("SELECT 1 FROM semantic_index LIMIT 1")
                self._vector_ok = True
            except Exception:  # noqa: BLE001
                self._vector_ok = False
                log.warning("pgvector unavailable — semantic recall falls back to keywords")
        return self._vector_ok

    # ------------------------------------------------------------- writing
    def index(self, ref: str, text: str, *, kind: str = KIND_FACT,
              meta: dict[str, Any] | None = None) -> bool:
        """Embed and store one item. Idempotent per (kind, ref) — re-indexing updates."""
        if kind not in KINDS or not (text or "").strip():
            return False
        try:
            vec = self.embedder.embed(text)
        except Exception:  # noqa: BLE001 - a broken embedder must not break the caller
            log.exception("embedding failed for %s", ref)
            return False
        literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        try:
            with self.mem.tx() as conn:
                conn.execute(
                    "INSERT INTO semantic_index(kind, ref, text, meta, embedding, dim) "
                    "VALUES(%s,%s,%s,%s,%s::vector,%s) "
                    "ON CONFLICT(kind, ref) DO UPDATE SET text=EXCLUDED.text, "
                    "meta=EXCLUDED.meta, embedding=EXCLUDED.embedding, dim=EXCLUDED.dim",
                    (kind, ref, text[:4000], json.dumps(meta or {}), literal, len(vec)))
            return True
        except Exception:  # noqa: BLE001
            log.exception("indexing failed for %s", ref)
            return False

    def index_many(self, items: list[tuple[str, str, dict[str, Any]]], *,
                   kind: str = KIND_FACT) -> int:
        return sum(1 for ref, text, meta in items if self.index(ref, text, kind=kind, meta=meta))

    def forget(self, ref: str, kind: str = KIND_FACT) -> None:
        """Drop an item from RECALL only. The underlying fact is untouched (§0 P4)."""
        with self.mem.tx() as conn:
            conn.execute("DELETE FROM semantic_index WHERE kind=%s AND ref=%s", (kind, ref))

    # ------------------------------------------------------------- reading
    def search(self, query: str, *, kind: str | None = None, k: int = 8,
               min_score: float = 0.0) -> list[Hit]:
        """Top-k by meaning. Falls back to keyword overlap when pgvector is unavailable."""
        if not (query or "").strip():
            return []
        if not self.vector_available():
            return self._keyword_search(query, kind=kind, k=k)
        try:
            vec = self.embedder.embed(query)
        except Exception:  # noqa: BLE001
            return self._keyword_search(query, kind=kind, k=k)
        literal = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
        sql = ("SELECT kind, ref, text, meta, 1 - (embedding <=> %s::vector) AS score "
               "FROM semantic_index")
        params: list[Any] = [literal]
        if kind:
            sql += " WHERE kind=%s"
            params.append(kind)
        # `<=>` is cosine distance; ORDER BY it is what the HNSW index accelerates.
        sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
        params += [literal, k]
        try:
            rows = self.mem.execute(sql, tuple(params)).fetchall()
        except Exception:  # noqa: BLE001
            log.exception("vector search failed — falling back to keywords")
            return self._keyword_search(query, kind=kind, k=k)
        return [self._hit(r) for r in rows if float(r["score"]) >= min_score]

    def _keyword_search(self, query: str, *, kind: str | None, k: int) -> list[Hit]:
        """Honest fallback: overlap of significant words. Worse than vectors, better than
        returning nothing and leaving the prompt empty."""
        words = {w.lower() for w in query.split() if len(w) > 3}
        if not words:
            return []
        sql = "SELECT kind, ref, text, meta FROM semantic_index"
        params: tuple[Any, ...] = ()
        if kind:
            sql += " WHERE kind=%s"
            params = (kind,)
        try:
            rows = self.mem.execute(sql, params).fetchall()
        except Exception:  # noqa: BLE001
            return []
        scored = []
        for r in rows:
            text_words = {w.lower().strip(".,:;()") for w in (r["text"] or "").split()}
            overlap = len(words & text_words)
            if overlap:
                scored.append((overlap / max(1, len(words)), r))
        scored.sort(key=lambda x: -x[0])
        return [self._hit(r, score) for score, r in scored[:k]]

    @staticmethod
    def _hit(row: Any, score: float | None = None) -> Hit:
        meta = row["meta"] if "meta" in row.keys() else {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta or "{}")
            except json.JSONDecodeError:
                meta = {}
        return Hit(ref=row["ref"], kind=row["kind"], text=row["text"],
                   score=float(score if score is not None else row.get("score", 0.0)),
                   meta=meta or {})

    # ------------------------------------------------------------- convenience
    def recall_text(self, query: str, *, kind: str | None = None, k: int = 8,
                    budget_chars: int = 2000, min_score: float = MIN_RELEVANCE) -> str:
        """The prompt-facing call: the most relevant items, capped by a character budget.

        A budget rather than a raw top-k because what actually matters is how much of the
        context window this is allowed to consume. Ordered by relevance, so truncation drops
        the least relevant item rather than an arbitrary one.

        `min_score` matters as much as `k`: nearest-neighbour search always returns SOMETHING,
        so without a floor a query about a Node.js role still drags in his Spotify genres at
        score 0.000 — putting back exactly the noise this was built to remove.
        """
        out, used = [], 0
        for hit in self.search(query, kind=kind, k=k, min_score=min_score):
            line = hit.text.strip()
            if used + len(line) > budget_chars:
                break
            out.append(line)
            used += len(line)
        return "\n".join(out)


_default: SemanticMemory | None = None


def get_semantic(memory: Memory | None = None) -> SemanticMemory:
    global _default
    if memory is not None:
        return SemanticMemory(memory=memory)
    if _default is None:
        _default = SemanticMemory()
    return _default
