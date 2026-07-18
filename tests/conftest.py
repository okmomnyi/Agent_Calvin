"""Shared pytest fixtures.

Tests are offline for every external service (NIM, Gmail, Telegram, scrapers are all
mocked). The one real dependency is PostgreSQL: `mem` runs against a local test database,
giving each test its own schema for isolation. Point TEST_DATABASE_URL at it, e.g.
    postgresql://agentos:agentos@localhost:5433/agentos_test
"""

from __future__ import annotations

import os
import uuid

import pytest

from core.llm import LLMClient
from core.memory import Memory

TEST_DSN = os.getenv(
    "TEST_DATABASE_URL", "postgresql://agentos:agentos@localhost:5433/agentos_test")


class FakeLLM(LLMClient):
    """LLMClient whose transport is replaced by scripted responses (no network)."""

    def __init__(self, classify_result: str = "chit_chat", post_result: str = "{}"):
        # deliberately skip super().__init__ network/config wiring
        self.classify_result = classify_result
        self.post_result = post_result
        self.calls: list[dict] = []
        self.routes = {"default": "fake-model"}
        self.defaults = {}

    def model_for(self, task: str) -> str:  # type: ignore[override]
        return "fake-model"

    def _post(self, model, messages, **params):  # type: ignore[override]
        self.calls.append({"model": model, "messages": list(messages), "params": params})
        return self.post_result

    def classify(self, text, labels, *, instruction="", task="classify"):  # type: ignore[override]
        self.calls.append({"classify": text, "labels": list(labels)})
        return self.classify_result


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture(autouse=True)
def _no_network_embeddings(monkeypatch, request):
    """The suite is offline. `vault.embedder: auto` now resolves to NIM, so any test touching
    semantic recall would quietly make a network call -- slow, flaky, and a breach of the
    guarantee that `pytest` needs no API keys. Pinned to the deterministic hashing embedder;
    mark a test @pytest.mark.allow_embedding_api to opt out."""
    if "allow_embedding_api" in request.keywords:
        return
    from core.embeddings import HashingEmbedder

    monkeypatch.setattr("core.embeddings.get_embedder", lambda *a, **k: HashingEmbedder())


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, request):
    """Nothing in the suite may push to the real Telegram. Autouse, so it cannot be forgotten.

    This is not hypothetical. Three tests called skill methods whose `notify` defaults to True
    -- job_hunter.interview_check, semester_planner.extract_deadlines,
    lecture_capture.process_inbox -- and `core.notify.send_telegram` reads the real bot token
    straight out of .env. Every full-suite run therefore fired live messages at Calvin's
    phone: "Interview invite detected! From: hr@acme.com", a lecture he never recorded, a
    deadline that does not exist. It ran for a whole night before he showed us the chat log.

    Patching the individual call sites would fix today and rot tomorrow, because the default
    stays notify=True and the next test to forget re-arms it. So the transport itself is
    severed for the whole session: a test that forgets now gets a loud failure instead of
    texting a human being.

    `allow_telegram` opts a test back in (nothing does today) for testing the sender itself.
    """
    if "allow_telegram" in request.keywords:
        return

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "A test tried to send a REAL Telegram message. Pass notify=False, or inject a "
            "fake notifier. (If you are testing the sender itself, mark it @pytest.mark."
            "allow_telegram.)")

    monkeypatch.setattr("core.notify.send_telegram", _blocked)
    # Skills import `notify`/`send_telegram` by value at module import, so patching only
    # core.notify leaves those bound to the original function. Rebind every alias.
    import importlib
    import pkgutil

    import skills as skills_pkg

    for mod_info in pkgutil.iter_modules(skills_pkg.__path__):
        for name in (f"skills.{mod_info.name}", f"skills.{mod_info.name}.skill"):
            try:
                mod = importlib.import_module(name)
            except Exception:  # noqa: BLE001 - not every skill is a package
                continue
            for attr in ("send_telegram", "notify"):
                if getattr(mod, attr, None) is not None and callable(getattr(mod, attr)):
                    monkeypatch.setattr(f"{name}.{attr}", _blocked, raising=False)


@pytest.fixture(scope="session")
def _db() -> Memory:
    """One Postgres schema + table set for the whole session (creating it per test is slow)."""
    schema = f"t_{uuid.uuid4().hex[:8]}"
    m = Memory(dsn=TEST_DSN, schema=schema)
    # test data is disposable — skip the fsync on every commit/truncate
    m.conn.execute("SET synchronous_commit TO off")
    yield m
    try:
        m.conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        m.close()


@pytest.fixture
def mem(_db) -> Memory:
    """A clean Memory for each test.

    Truncating the session schema is far cheaper than recreating it, and RESTART IDENTITY
    keeps row ids deterministic per test. The truncate lives here — NOT on Memory — because
    the production data layer must never expose a bulk-delete (§0 Principle 4).
    """
    tables = [r["tablename"] for r in _db.conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()").fetchall()]
    if tables:
        names = ", ".join(f'"{t}"' for t in tables)
        _db.conn.execute(f"TRUNCATE {names} RESTART IDENTITY CASCADE")
    return _db


class _SettingsProxy:
    """Wraps the real Settings but lets a test override auto_apply without touching env."""

    def __init__(self, real, auto_apply: bool):
        self._real = real
        self.auto_apply = auto_apply

    def __getattr__(self, name):  # only hit for attributes not set on the proxy
        return getattr(self._real, name)


@pytest.fixture
def fake_settings(monkeypatch):
    from core.config import get_settings

    proxy = _SettingsProxy(get_settings(), auto_apply=False)
    monkeypatch.setattr("skills.job_hunter.skill.get_settings", lambda: proxy)
    return proxy


@pytest.fixture
def fake_settings_autoapply(monkeypatch):
    from core.config import get_settings

    proxy = _SettingsProxy(get_settings(), auto_apply=True)
    monkeypatch.setattr("skills.job_hunter.skill.get_settings", lambda: proxy)
    return proxy
