"""Per-task LLM routing tests: model/key/endpoint/params resolve per task class, with
graceful fallback to the default key when a task-specific key isn't set."""

from __future__ import annotations

import types

from core.llm import LLMClient


def _client(routes, base_url="https://nim.default/v1", default_key="DEFAULT_KEY"):
    c = LLMClient.__new__(LLMClient)          # bypass __init__ / real settings
    c.settings = types.SimpleNamespace(nvidia_api_key=default_key)
    c.base_url = base_url
    c.routes = routes
    c.defaults = {}
    c.default_key_env = "NVIDIA_API_KEY"
    return c


def test_string_route_uses_default_key_and_endpoint():
    c = _client({"default": "d", "write": "kimi"})
    r = c.resolve_route("write")
    assert r.model == "kimi"
    assert r.api_key == "DEFAULT_KEY"                 # no per-task key -> default
    assert r.base_url == "https://nim.default/v1"


def test_unknown_task_falls_back_to_default_route():
    c = _client({"default": "meta/llama"})
    assert c.resolve_route("nonexistent").model == "meta/llama"


def test_dict_route_resolves_model_params():
    c = _client({"research": {"model": "deepseek-r1", "max_tokens": 1500, "temperature": 0.1}})
    r = c.resolve_route("research")
    assert r.model == "deepseek-r1"
    assert r.params == {"max_tokens": 1500, "temperature": 0.1}


def test_per_task_key_env_is_used_when_set(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY_CODE", "CODE_KEY_123")
    c = _client({"code_review": {"model": "qwen-coder", "api_key_env": "NVIDIA_API_KEY_CODE"}})
    r = c.resolve_route("code_review")
    assert r.api_key == "CODE_KEY_123"                # task-specific key used


def test_per_task_key_env_falls_back_when_unset(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY_CODE", raising=False)
    c = _client({"code_review": {"model": "qwen-coder", "api_key_env": "NVIDIA_API_KEY_CODE"}})
    assert c.resolve_route("code_review").api_key == "DEFAULT_KEY"   # graceful fallback


def test_per_task_base_url_override():
    c = _client({"research": {"model": "r1", "base_url": "https://other.endpoint/v1"}})
    assert c.resolve_route("research").base_url == "https://other.endpoint/v1"


def test_post_routed_passes_key_and_endpoint():
    captured = {}

    class _C(LLMClient):
        def __init__(self):
            self.settings = types.SimpleNamespace(nvidia_api_key="DEFAULT_KEY")
            self.base_url = "https://nim.default/v1"
            self.routes = {"code_review": {"model": "qwen", "base_url": "https://code.ep/v1"}}
            self.defaults = {}
            self.default_key_env = "NVIDIA_API_KEY"

        def _post(self, model, messages, *, api_key=None, base_url=None, **params):
            captured.update(model=model, api_key=api_key, base_url=base_url, params=params)
            return "ok"

    _C().chat("code_review", [{"role": "user", "content": "hi"}], temperature=0.2)
    assert captured["model"] == "qwen"
    assert captured["base_url"] == "https://code.ep/v1"
    assert captured["api_key"] == "DEFAULT_KEY"
    assert captured["params"]["temperature"] == 0.2   # per-call param passed through
