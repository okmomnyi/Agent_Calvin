"""Semantic memory (Phase 33) — retrieve what's relevant instead of stuffing everything.

Every CV tailor used to carry all 23 verified facts (~1,181 tokens) regardless of the job, so
a Node.js role received Calvin's Docker work, his Spotify genres and his collaborator list.
That is noise for the model to weigh, latency on every call, and it grows with every fact
learned — the system would degrade as it improved.

These tests pin the properties that make recall trustworthy: it must return the RIGHT things,
exclude the irrelevant ones, and never crash a prompt when the vector store is missing.
"""

from __future__ import annotations

import pytest

from core.semantic import KIND_FACT, KIND_NOTE, MIN_RELEVANCE, SemanticMemory


@pytest.fixture
def sem(mem):
    """Deterministic, offline embedder.

    NOT the configured one: `auto` now resolves to NIM, so these tests were quietly making
    network calls -- slow, flaky, and a breach of the suite's offline guarantee. Recall
    QUALITY is a property of the embedding model and belongs in a benchmark; what these tests
    pin is the retrieval LOGIC, which must hold for any embedder.
    """
    from core.embeddings import HashingEmbedder

    return SemanticMemory(memory=mem, embedder=HashingEmbedder())


FACTS = [
    ("f1", "[tools] Docker, containerisation, PostgreSQL, Linux shell scripting", {}),
    ("f2", "[music] top genres: afrobeats, kenyan pop, amapiano", {}),
    ("f3", "[skills] React, Vite, Tailwind CSS, TypeScript frontend work", {}),
]


def test_indexing_is_idempotent_per_ref(sem, mem):
    assert sem.index("f1", "first version", kind=KIND_FACT)
    assert sem.index("f1", "second version", kind=KIND_FACT)
    rows = mem.execute("SELECT text FROM semantic_index WHERE ref='f1'").fetchall()
    assert len(rows) == 1 and rows[0]["text"] == "second version"


def test_recall_returns_the_relevant_fact_and_drops_the_rest(sem):
    sem.index_many(FACTS, kind=KIND_FACT)
    recalled = sem.recall_text("DevOps role: Docker, Linux, PostgreSQL",
                               kind=KIND_FACT, budget_chars=2000)
    assert "Docker" in recalled
    assert "amapiano" not in recalled, "an unrelated fact was pulled into the prompt"


def test_recall_is_materially_smaller_than_stuffing(sem):
    """The point of the phase: fewer tokens, and the ones that remain are the right ones."""
    sem.index_many(FACTS, kind=KIND_FACT)
    everything = sum(len(f[1]) for f in FACTS)
    recalled = sem.recall_text("Docker and Linux infrastructure", kind=KIND_FACT)
    assert 0 < len(recalled) < everything


def test_a_zero_relevance_hit_is_excluded(sem):
    """Nearest-neighbour ALWAYS returns something; without a floor the noise comes back."""
    sem.index_many(FACTS, kind=KIND_FACT)
    hits = sem.search("Docker containers", kind=KIND_FACT, k=3, min_score=MIN_RELEVANCE)
    assert all(h.score >= MIN_RELEVANCE for h in hits)
    assert not any("amapiano" in h.text for h in hits)


def test_kinds_are_separate_namespaces(sem):
    sem.index("a", "Docker and Linux", kind=KIND_FACT)
    sem.index("b", "Docker and Linux", kind=KIND_NOTE)
    assert all(h.kind == KIND_FACT for h in sem.search("docker", kind=KIND_FACT, k=5))


def test_an_unknown_kind_is_refused(sem):
    assert sem.index("x", "text", kind="not_a_kind") is False


def test_empty_text_is_not_indexed(sem):
    assert sem.index("x", "   ", kind=KIND_FACT) is False


def test_forgetting_removes_from_recall_only(sem, mem):
    """§0 P4: dropping something from RECALL must not touch the underlying fact."""
    sem.index("f1", FACTS[0][1], kind=KIND_FACT)
    sem.forget("f1", kind=KIND_FACT)
    assert sem.search("docker", kind=KIND_FACT, k=5) == []
    # the persona/cv fact itself lives in its own table, untouched by this call
    assert mem.execute("SELECT COUNT(*) c FROM semantic_index").fetchone()["c"] == 0


def test_a_broken_embedder_never_breaks_the_caller(sem):
    class _Boom:
        dim = 1024

        def embed(self, text):
            raise RuntimeError("embedding service down")

        def embed_batch(self, texts):
            raise RuntimeError("embedding service down")

    sem._embedder = _Boom()
    assert sem.index("f9", "some text", kind=KIND_FACT) is False   # reported, not raised
    assert sem.search("anything", kind=KIND_FACT) == []            # empty, not an exception


def test_recall_degrades_to_keywords_without_pgvector(sem, monkeypatch):
    """A missing extension must make prompts slower, never wrong or empty."""
    sem.index_many(FACTS, kind=KIND_FACT)
    monkeypatch.setattr(sem, "vector_available", lambda: False)
    recalled = sem.recall_text("Docker Linux PostgreSQL", kind=KIND_FACT)
    assert "Docker" in recalled, "keyword fallback returned nothing"
