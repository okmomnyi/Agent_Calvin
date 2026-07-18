"""NVIDIA NIM chat client with task-class routing for AgentOS.

All LLM traffic goes through this module (Principle 1, §0: free-first NIM stack).
Every call declares a task class (classify/write/persona/code_review/research/voice_chat);
the router maps class -> model via config.yaml (Principle 2: routing, never hardcoding).

Per-task routing is rich: each class can declare its own model, its OWN API key (env var),
its own base_url, and its own generation params. That means the strongest model is always
used for each kind of work (a coder model for code_review, a reasoning model for research,
a cheap model for classify) AND concurrent work spreads across separate rate-limit buckets
— e.g. the scheduler, the Telegram bot, and a voice request can each hit a different key at
the same time without throttling one another. Any unset per-task key falls back to the
default NVIDIA_API_KEY, so a single key still works out of the box.

Provides retries with 429/Retry-After backoff, a strict-single-label classify() helper,
and a chat_json() helper that strips code fences and retries once on parse failure.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import requests

from core.config import Settings, get_settings
from core.logging_setup import get_logger

log = get_logger("core.llm")

Message = dict[str, str]

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"


class LLMError(RuntimeError):
    """Raised when the NIM API cannot be reached or returns an unusable response."""


@dataclass
class Route:
    """Resolved routing for one task class: which model, key, endpoint, and params to use."""

    task: str
    model: str
    api_key: str
    base_url: str
    params: dict[str, Any]


class LLMClient:
    """Thin, retrying client over the NIM /chat/completions endpoint with model routing."""

    def __init__(
        self,
        settings: Settings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.session = session or requests.Session()
        llm = self.settings.llm
        self.base_url: str = llm.get("base_url", "https://integrate.api.nvidia.com/v1").rstrip("/")
        self.routes: dict[str, Any] = self.settings.llm_routes
        self.defaults: dict[str, Any] = llm.get("defaults", {})
        self.default_key_env: str = llm.get("default_api_key_env", "NVIDIA_API_KEY")
        req = llm.get("request", {})
        self.timeout: int = int(req.get("timeout_seconds", 60))
        self.max_retries: int = int(req.get("max_retries", 4))
        self.backoff_base: float = float(req.get("backoff_base_seconds", 1.5))

    # ------------------------------------------------------------------ routing
    def resolve_route(self, task: str) -> Route:
        """Resolve a task class to (model, api_key, base_url, params).

        A route value in config.yaml may be a bare model string, or a dict:
        `{model, api_key_env, base_url, temperature, max_tokens}`. Unset fields fall back to
        the defaults (default key env, default base_url, default generation params).
        """
        raw = self.routes.get(task)
        if raw is None:
            raw = self.routes.get("default", _DEFAULT_MODEL)
        if isinstance(raw, str):
            model, key_env, base_url, extra = raw, None, None, {}
        else:
            model = raw.get("model", _DEFAULT_MODEL)
            key_env = raw.get("api_key_env")
            base_url = raw.get("base_url")
            extra = {k: raw[k] for k in ("temperature", "max_tokens", "stop") if k in raw}
        return Route(task=task, model=model, api_key=self._resolve_key(key_env),
                     base_url=(base_url or getattr(self, "base_url", None)
                               or "https://integrate.api.nvidia.com/v1").rstrip("/"),
                     params=extra)

    def _resolve_key(self, key_env: str | None) -> str:
        """Return the API key for a route: the per-task env var if set, else the default key."""
        if key_env:
            val = os.getenv(key_env)
            if val:
                return val
        settings = getattr(self, "settings", None)
        if settings is not None:
            default_env = getattr(self, "default_key_env", "NVIDIA_API_KEY")
            if default_env != "NVIDIA_API_KEY":
                env_val = os.getenv(default_env)
                if env_val:
                    return env_val
            return getattr(settings, "nvidia_api_key", "")
        return ""

    def model_for(self, task: str) -> str:
        """Resolve a task class to a model id, falling back to the `default` route."""
        return self.resolve_route(task).model

    # ------------------------------------------------------------------ transport
    def _post(self, model: str, messages: Sequence[Message], *, api_key: str | None = None,
              base_url: str | None = None, **params: Any) -> str:
        """POST one completion request with retry/backoff. Returns assistant text.

        `api_key`/`base_url` come from the resolved Route (per-task key + endpoint). Isolated
        so tests can monkeypatch this single method to avoid real network calls.
        """
        key = api_key or getattr(self.settings, "nvidia_api_key", "")
        if not key:
            raise LLMError("No API key resolved for this task — set NVIDIA_API_KEY (or the "
                           "task's api_key_env) in .env.")

        base = (base_url or self.base_url).rstrip("/")
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": params.get("temperature", self.defaults.get("temperature", 0.4)),
            "max_tokens": params.get("max_tokens", self.defaults.get("max_tokens", 1024)),
        }
        if "stop" in params:
            body["stop"] = params["stop"]

        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.post(url, headers=headers, json=body, timeout=self.timeout)
            except requests.RequestException as exc:  # network-level failure
                last_err = exc
                self._sleep(attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                last_err = LLMError(f"NIM {resp.status_code}: {resp.text[:200]}")
                self._sleep(attempt, retry_after)
                continue

            if resp.status_code != 200:
                raise LLMError(f"NIM {resp.status_code}: {resp.text[:300]}")

            try:
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError) as exc:
                raise LLMError(f"Unexpected NIM response shape: {exc}") from exc

        raise LLMError(f"NIM request failed after {self.max_retries} attempts: {last_err}")

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), 30.0))
                return
            except (TypeError, ValueError):
                pass
        time.sleep(min(self.backoff_base * (2 ** attempt), 30.0))

    # ------------------------------------------------------------------ public API
    def _post_routed(self, task: str, messages: Sequence[Message], **params: Any) -> str:
        """Resolve the task's route (model/key/endpoint/params) and POST.

        If the task's model fails after its retries, fall back ONCE to the default route
        rather than failing the whole feature. A single wrong/gated model id (mistral-medium
        -3.5-128b and deepseek-v4-pro were both dead on this account) otherwise silently broke
        CV tailoring, cover letters, briefings and the tutor with "Couldn't ... right now".
        Degrading to the default model keeps the feature working; the miss is logged loudly.
        """
        route = self.resolve_route(task)
        merged = {**route.params, **params}   # per-call params win over per-route defaults
        log.debug("llm task=%s model=%s endpoint=%s", task, route.model, route.base_url)
        try:
            return self._post(route.model, messages, api_key=route.api_key,
                              base_url=route.base_url, **merged)
        except LLMError:
            default = self.resolve_route("default")
            if default.model == route.model:
                raise            # already the default; nothing to fall back to
            log.error("llm task=%s model=%s failed — falling back to default model %s",
                      task, route.model, default.model)
            return self._post(default.model, messages, api_key=default.api_key,
                              base_url=default.base_url, **{**default.params, **params})

    def chat(self, task: str, messages: Sequence[Message], **params: Any) -> str:
        """Route by task class and return the assistant's text response."""
        return self._post_routed(task, self._grounded(messages), **params).strip()

    @staticmethod
    def _grounded(messages: Sequence[Message]) -> list[Message]:
        """Prepend current-time and evidence rules to every generative call."""
        from core.time_context import runtime_truth

        return [{"role": "system", "content": runtime_truth()}, *list(messages)]

    def classify(
        self,
        text: str,
        labels: Iterable[str],
        *,
        instruction: str = "",
        task: str = "classify",
    ) -> str:
        """Return exactly one label from `labels` for `text` (strict single-label).

        Falls back to the first label if the model returns something off-menu, so callers
        always get a valid label.
        """
        label_list = list(labels)
        menu = ", ".join(label_list)
        sys = (
            "You are a strict single-label classifier. "
            "Respond with EXACTLY ONE label from the allowed set and nothing else. "
            "No punctuation, no explanation."
        )
        if instruction:
            sys += " " + instruction
        user = f"Allowed labels: {menu}\n\nText:\n{text}\n\nLabel:"
        raw = self._post_routed(
            task,
            [{"role": "system", "content": sys}, {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=16,
        ).strip()

        norm = raw.strip().strip(".").lower()
        for label in label_list:
            if norm == label.lower():
                return label
        for label in label_list:  # lenient contains-match
            if label.lower() in norm:
                return label
        log.warning("classify: off-menu response %r, defaulting to %r", raw, label_list[0])
        return label_list[0]

    def chat_json(
        self,
        task: str,
        messages: Sequence[Message],
        schema_hint: str,
        **params: Any,
    ) -> dict[str, Any]:
        """Return a parsed JSON object. Strips code fences; retries once on parse failure."""
        msgs = self._grounded(messages)
        instruction = (
            "Respond with a single valid JSON object only. No prose, no code fences. "
            f"Schema: {schema_hint}"
        )
        msgs.append({"role": "system", "content": instruction})

        raw = self._post_routed(task, msgs, **params)
        parsed = _try_parse_json(raw)
        if parsed is not None:
            return parsed

        # one repair attempt
        repair = list(msgs) + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "That was not valid JSON. Return ONLY the corrected JSON object.",
            },
        ]
        raw2 = self._post_routed(task, repair, **params)
        parsed2 = _try_parse_json(raw2)
        if parsed2 is not None:
            return parsed2
        raise LLMError(f"chat_json could not parse a JSON object. Last response: {raw2[:300]}")


def _strip_fences(text: str) -> str:
    """Remove a leading ```json / ``` fence and a trailing ``` fence if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # drop the opening fence line and any closing fence
        stripped = _FENCE_RE.sub("", stripped)
    return stripped.strip()


def _try_parse_json(text: str) -> dict[str, Any] | None:
    candidate = _strip_fences(text)
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        # last resort: grab the outermost {...} span
        start, end = candidate.find("{"), candidate.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(candidate[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None


_default_client: LLMClient | None = None


def get_client() -> LLMClient:
    """Return a lazily-constructed process-wide LLM client."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
