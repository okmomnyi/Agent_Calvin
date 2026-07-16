"""LLM router / client tests: task->model routing, classify strictness, JSON parsing."""

from __future__ import annotations

import json

import pytest

from core.llm import LLMClient, _try_parse_json, _strip_fences


class _RoutedClient(LLMClient):
    """LLMClient with routes injected and network disabled, to test routing purely."""

    def __init__(self, routes):
        self.routes = routes
        self.defaults = {}
        self.last_model = None

    def _post(self, model, messages, **params):  # type: ignore[override]
        self.last_model = model
        return "ok"


def test_model_routing_falls_back_to_default():
    c = _RoutedClient({"default": "d", "code_review": "coder", "classify": "cheap"})
    assert c.model_for("code_review") == "coder"
    assert c.model_for("classify") == "cheap"
    assert c.model_for("unknown_task") == "d"  # falls back to default


def test_chat_uses_routed_model():
    c = _RoutedClient({"default": "d", "write": "writer"})
    c.chat("write", [{"role": "user", "content": "hi"}])
    assert c.last_model == "writer"


def test_strip_fences_removes_json_block():
    fenced = "```json\n{\"a\": 1}\n```"
    assert _strip_fences(fenced) == '{"a": 1}'


def test_try_parse_json_handles_fenced_and_noisy():
    assert _try_parse_json('```json\n{"x": 2}\n```') == {"x": 2}
    assert _try_parse_json('here you go: {"y": 3} cheers') == {"y": 3}
    assert _try_parse_json("not json at all") is None


def test_chat_json_repairs_once():
    """First response is broken JSON; the repair attempt returns valid JSON."""

    class _Flaky(LLMClient):
        def __init__(self):
            self.routes = {"default": "d"}
            self.defaults = {}
            self._n = 0

        def _post(self, model, messages, **params):  # type: ignore[override]
            self._n += 1
            return "oops not json" if self._n == 1 else '{"ok": true}'

    c = _Flaky()
    out = c.chat_json("write", [{"role": "user", "content": "give json"}], schema_hint="{ok: bool}")
    assert out == {"ok": True}


def test_classify_returns_valid_label(fake_llm):
    # FakeLLM.classify is scripted; ensure it stays within the label set contract
    fake_llm.classify_result = "find_jobs"
    label = fake_llm.classify("any jobs?", ["find_jobs", "chit_chat"])
    assert label in {"find_jobs", "chit_chat"}
