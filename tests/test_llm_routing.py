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


def test_a_dead_model_falls_back_to_default_not_failure():
    """A wrong/gated model id must degrade to the default model, not break the feature.

    mistral-medium-3.5-128b (write) and deepseek-v4-pro (code) were both dead on the real NIM
    account, silently breaking CV tailoring and covers with "Couldn't ... right now". The
    fallback keeps those features working on the default model instead.
    """
    from core.llm import LLMClient, LLMError

    c = LLMClient()
    seen = []

    def fake_post(model, messages, **kw):
        seen.append(model)
        if model != c.resolve_route("default").model:
            raise LLMError("timeout on the fancy model")
        return "ok from default"

    c._post = fake_post
    out = c.chat("write", [{"role": "user", "content": "hi"}])
    assert out == "ok from default"
    assert seen[-1] == c.resolve_route("default").model      # ended on the default
    assert len(seen) == 2                                     # tried route, then default


def test_default_route_failure_is_not_retried_forever():
    """If even the default model fails, raise -- don't loop."""
    from core.llm import LLMClient, LLMError

    c = LLMClient()
    calls = []

    def always_fail(model, messages, **kw):
        calls.append(model)
        raise LLMError("everything is down")

    c._post = always_fail
    import pytest
    with pytest.raises(LLMError):
        c.chat("write", [{"role": "user", "content": "hi"}])
    assert len(calls) == 2        # route once, default once, then give up


def test_reasoning_model_content_is_recovered_not_treated_as_a_failure():
    """qwen3.5 returns `reasoning_content`; with a tight max_tokens `content` is absent.

    That raised KeyError('content') -> "Unexpected NIM response shape" -> the route was judged
    dead and every CV tailor and cover letter was silently demoted to the fallback model. The
    answer was there all along, in the other field.
    """
    from core.llm import LLMClient

    c = LLMClient()

    class _R:
        status_code = 200
        headers: dict = {}
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"reasoning_content": "the answer"},
                                 "finish_reason": "length"}]}

    c.session = type("S", (), {"post": staticmethod(lambda *a, **k: _R())})()
    assert c._post("qwen/qwen3.5-122b-a10b", [{"role": "user", "content": "hi"}]) == "the answer"


def test_a_genuinely_empty_response_says_why():
    """No content and no reasoning: the error must name the likely cause, not just 'shape'."""
    import pytest

    from core.llm import LLMClient, LLMError

    c = LLMClient()

    class _R:
        status_code = 200
        headers: dict = {}
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "  "}, "finish_reason": "length"}]}

    c.session = type("S", (), {"post": staticmethod(lambda *a, **k: _R())})()
    with pytest.raises(LLMError, match="max_tokens"):
        c._post("qwen/qwen3.5-122b-a10b", [{"role": "user", "content": "hi"}], max_tokens=24)
