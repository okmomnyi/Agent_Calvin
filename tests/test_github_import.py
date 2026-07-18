"""GitHub persona import (Phase 25).

Calvin's 45 public repos are better evidence of what he builds than any interview answer.
But the rule that makes this safe is narrow:

    A MACHINE READING A README DOES NOT GET TO DECIDE WHAT IS TRUE ABOUT CALVIN.

Everything derived lands UNVERIFIED and waits for him. "Experimenting with Kubernetes" in a
README must never reach an employer as "uses Kubernetes" (§0 Principle 5).

Also pinned here: GitHub only. LinkedIn forbids automated access and blocks it, and the
account that gets restricted is his -- the same call config.yaml already makes about
Facebook Marketplace.

No network: the HTTP layer and the LLM are injected.
"""

from __future__ import annotations

import base64
import json

import pytest

from core.github_profile import Repo, evidence, profile, repos
from core.persona_store import PersonaEngine
from skills.persona import _GH_CATEGORIES, PersonaSkill


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text

    def json(self):
        return self._p


def _fake_http(**overrides):
    """Stand-in for the GitHub REST API."""
    repos_json = [
        {"name": "Agent_Calvin", "language": "Python", "description": None, "fork": False,
         "topics": ["agent"], "stargazers_count": 1, "pushed_at": "2026-07-17T00:00:00Z"},
        {"name": "ZameenEye-AI", "language": "TypeScript", "fork": False, "topics": [],
         "description": "automated translation layer", "stargazers_count": 0, "pushed_at": ""},
        {"name": "somebody-elses-repo", "language": "Go", "fork": True, "topics": [],
         "description": "a fork", "stargazers_count": 0, "pushed_at": ""},
    ]
    pages = {
        "/users/okmomnyi": {"login": "okmomnyi", "name": "Kelvin Momanyi", "bio": None,
                            "blog": "https://www.kelvinmomanyi.codes/", "public_repos": 45},
        "/users/okmomnyi/repos?per_page=100&sort=pushed": repos_json,
    }
    pages.update(overrides)

    def http(url, **kw):
        path = url.replace("https://api.github.com", "")
        if path.endswith("/readme"):
            body = base64.b64encode(b"# Agent_Calvin\nFastAPI + Postgres.").decode()
            return _Resp({"content": body})
        if path in pages:
            return _Resp(pages[path])
        return _Resp({}, status=404, text="not found")
    return http


class _GhLLM:
    """LLM that returns whatever facts the test wants."""

    def __init__(self, facts):
        self.routes, self.defaults = {"write": "m", "research": "m"}, {}
        self._facts = facts
        self.saw = None

    def chat_json(self, task, messages, schema_hint, **kw):
        self.system = messages[0]["content"]
        self.saw = messages[-1]["content"]
        return {"facts": self._facts}


def _skill(mem, facts=None, gh=None):
    engine = PersonaEngine(llm=None, memory=mem)
    llm = _GhLLM(facts if facts is not None else [
        {"category": "skills", "key": "typescript", "value": "TypeScript across 14 repos",
         "evidence": "ZameenEye-AI"}])
    return PersonaSkill(engine=engine, notify=lambda t: True, llm=llm,
                        gh_evidence=gh or (lambda u: "GitHub: okmomnyi\nRepos: ...")), engine, llm


# ================================================================= the guarantee
def test_imported_facts_are_candidates_never_verified(mem):
    skill, engine, _ = _skill(mem)
    res = skill.import_github(user="okmomnyi", notify=False)
    assert res.data["candidates"] == 1
    facts = engine.get_facts()
    assert facts and all(not f["verified"] for f in facts), \
        "a README must not become a verified claim about Calvin"
    assert engine.get_facts(verified_only=True) == []


def test_an_unverified_import_does_not_seed_the_persona(mem):
    """is_seeded() gates cover letters. GitHub alone must not flip it."""
    from core.persona_store import is_seeded

    skill, _, _ = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    assert is_seeded(mem) is False, "covers would start claiming unconfirmed GitHub inferences"


def test_covers_still_refuse_until_calvin_confirms(mem):
    from core.persona_store import verified_facts_text

    skill, engine, _ = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    assert verified_facts_text(mem) == "", "unconfirmed facts leaked into the cover prompt"
    skill.verify(category="skills", key="typescript", accept=True)
    assert "TypeScript" in verified_facts_text(mem)


def test_confirming_promotes_and_rejecting_does_not_delete(mem):
    skill, engine, _ = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    skill.verify(category="skills", key="typescript", accept=False)
    assert engine.get_facts(verified_only=True) == []
    assert engine.get_facts(), "§0 P4: a rejected fact is deactivated, never deleted"


def test_candidates_lists_what_is_waiting(mem):
    skill, _, _ = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    out = skill.candidates()
    assert out.data["count"] == 1 and "typescript" in out.text
    skill.verify(category="skills", key="typescript", accept=True)
    assert skill.candidates().data["count"] == 0


# ================================================================= what GitHub may speak to
def test_rates_and_availability_are_not_importable(mem):
    """No repository knows Calvin's day-rate or notice period; a model asked would invent them."""
    for banned in ("rates", "availability", "work_authorization"):
        assert banned not in _GH_CATEGORIES


def test_facts_in_disallowed_categories_are_dropped(mem):
    skill, engine, _ = _skill(mem, facts=[
        {"category": "rates", "key": "day_rate", "value": "$400/day", "evidence": "vibes"},
        {"category": "skills", "key": "python", "value": "Python", "evidence": "Agent_Calvin"},
    ])
    skill.import_github(user="okmomnyi", notify=False)
    keys = [f["key"] for f in engine.get_facts()]
    assert "day_rate" not in keys, "an invented day-rate reached the persona"
    assert "python" in keys


def test_provenance_is_recorded_on_every_fact(mem):
    skill, engine, _ = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    f = engine.get_facts()[0]
    assert f["source"] == "github:okmomnyi"
    assert f["confidence"] < 0.9, "machine-read facts must not claim interview-level confidence"
    assert "from GitHub" in f["value"], "the fact should say where it came from"


def test_the_model_is_told_not_to_claim_aspirations(mem):
    """The instruction that stops "experimenting with K8s" becoming "uses K8s"."""
    skill, _, llm = _skill(mem)
    skill.import_github(user="okmomnyi", notify=False)
    sys_prompt = llm.system.lower()
    assert "learning" in sys_prompt and "experimenting" in sys_prompt
    assert "do not claim" in sys_prompt
    # and it must not be invited to guess seniority from a repo count
    assert "never infer seniority" in sys_prompt


# ================================================================= the API client
def test_forks_are_not_presented_as_his_work():
    rs = repos("okmomnyi", http=_fake_http(), with_readmes=0)
    names = [r.name for r in rs]
    assert "somebody-elses-repo" not in names, "a fork is someone else's code"
    assert "Agent_Calvin" in names


def test_evidence_contains_only_what_github_returned():
    ev = evidence("okmomnyi", http=_fake_http())
    assert "okmomnyi" in ev and "TypeScript" in ev and "Agent_Calvin" in ev
    assert "somebody-elses-repo" not in ev


def test_rate_limit_is_reported_not_swallowed():
    from core.github_profile import GitHubError

    def limited(url, **kw):
        return _Resp({}, status=403, text="API rate limit exceeded for 1.2.3.4")

    with pytest.raises(GitHubError, match="rate limit"):
        profile("okmomnyi", http=limited)


def test_a_wrong_username_says_so():
    from core.github_profile import GitHubError

    with pytest.raises(GitHubError, match="404"):
        profile("definitely-not-a-user-xyz", http=_fake_http())


def test_import_failure_is_reported_not_raised(mem):
    def boom(user):
        raise RuntimeError("GitHub rate limit hit")

    skill, _, _ = _skill(mem, gh=boom)
    res = skill.import_github(user="okmomnyi", notify=False)
    assert res.ok is False and "rate limit" in res.text


# ================================================================= linkedin stays out
def test_nothing_here_touches_linkedin():
    """LinkedIn forbids automated access and blocks it -- and it is Calvin's account that gets
    restricted. Same call config.yaml already makes about Facebook Marketplace. If he wants
    LinkedIn content in his persona he pastes it and `remember`s it."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for f in (root / "core" / "github_profile.py", root / "skills" / "persona.py"):
        src = f.read_text(encoding="utf-8").lower()
        assert "linkedin.com" not in src or "forbids" in src or "blocks" in src, \
            f"{f.name} appears to fetch LinkedIn"
        assert "api.linkedin" not in src


# ================================================================= detailed / deterministic
def _detailed_http():
    """Fake API covering own repos + two collaboration repos with contributor graphs."""
    import base64 as _b64

    own = [
        {"name": "Agent_Calvin", "language": "Python", "fork": False, "topics": [],
         "description": "24/7 agentic system", "homepage": "", "stargazers_count": 1,
         "pushed_at": "2026-07-18T00:00:00Z"},
        {"name": "Forge-Vault", "language": "JavaScript", "fork": False, "topics": [],
         "description": None, "homepage": "https://forge-vault.vercel.app",
         "stargazers_count": 0, "pushed_at": ""},
        {"name": "a-fork", "language": "Go", "fork": True, "topics": [], "description": None,
         "homepage": "", "stargazers_count": 0, "pushed_at": ""},
    ]
    pages = {
        "/users/okmomnyi": {"login": "okmomnyi", "name": "Kelvin Momanyi", "bio": None,
                            "blog": "https://kelvinmomanyi.codes", "public_repos": 45},
        "/users/okmomnyi/repos?per_page=100&sort=pushed": own,
        "/repos/Techtoxic/ums": {"description": None, "language": "HTML",
                                 "homepage": "https://ums-three-mu.vercel.app"},
        "/repos/Techtoxic/ums/contributors?per_page=15": [
            {"login": "Techtoxic", "contributions": 4}, {"login": "okmomnyi", "contributions": 3}],
        "/repos/David-0chieng/Project47": {"description": "team build", "language": "TypeScript",
                                           "homepage": "https://project47.vercel.app"},
        "/repos/David-0chieng/Project47/contributors?per_page=15": [
            {"login": "David-0chieng", "contributions": 10},
            {"login": "okmomnyi", "contributions": 7},
            {"login": "philipkiema6", "contributions": 1}],
    }

    def http(url, **kw):
        path = url.replace("https://api.github.com", "")
        if path in pages:
            return _Resp(pages[path])
        return _Resp({}, status=404, text="nf")
    return http


def test_collaborations_report_commits_and_teammates():
    from core.github_profile import collaborations

    cs = collaborations("okmomnyi", ["Techtoxic/ums", "David-0chieng/Project47"],
                        http=_detailed_http())
    by_name = {c.full_name: c for c in cs}
    assert by_name["David-0chieng/Project47"].my_commits == 7
    assert "David-0chieng" in by_name["David-0chieng/Project47"].teammates
    assert "okmomnyi" not in by_name["David-0chieng/Project47"].teammates  # not his own teammate


def test_derive_facts_is_deterministic_and_factual():
    from core.github_profile import derive_facts

    facts = derive_facts("okmomnyi", ["Techtoxic/ums", "David-0chieng/Project47"],
                         http=_detailed_http())
    keyed = {f["key"]: f["value"] for f in facts}
    # languages counted from own repos only (fork excluded)
    assert "languages_by_repo_count" in keyed
    assert "Go" not in keyed["languages_by_repo_count"]        # the fork's language is not his
    # a live deployment surfaced
    assert "collab_ums" in keyed and "ums-three-mu" in keyed["collab_ums"]
    assert "7 commit" in keyed["collab_project47"]
    # aggregate collaborators, himself excluded
    assert "collaborators" in keyed and "philipkiema6" in keyed["collaborators"]
    assert "okmomnyi" not in keyed["collaborators"]


def test_detailed_import_seeds_candidates_never_verified(mem):
    import core.github_profile as gp
    from core.persona_store import PersonaEngine, is_seeded
    from skills.persona import PersonaSkill

    engine = PersonaEngine(llm=None, memory=mem)
    skill = PersonaSkill(engine=engine, notify=lambda t: True)
    skill.import_github_detailed = skill.import_github_detailed  # bind
    # inject the fake http by monkeypatching derive_facts via the module
    orig = gp.derive_facts
    gp.derive_facts = lambda user, collab=None, **kw: orig(
        user, ["Techtoxic/ums"], http=_detailed_http())
    try:
        res = skill.import_github_detailed(user="okmomnyi", notify=False)
    finally:
        gp.derive_facts = orig
    assert res.data["candidates"] >= 3
    assert all(not f["verified"] for f in engine.get_facts())
    assert is_seeded(mem) is False        # detailed import alone must not seed


def test_bots_are_not_listed_as_teammates():
    from core.github_profile import collaborations

    http = _detailed_http.__wrapped__ if hasattr(_detailed_http, "__wrapped__") else None
    # add Copilot to the ums contributor list
    def with_bot(url, **kw):
        base = _detailed_http()
        path = url.replace("https://api.github.com", "")
        if path == "/repos/Techtoxic/ums/contributors?per_page=15":
            return _Resp([{"login": "Techtoxic", "contributions": 4},
                          {"login": "okmomnyi", "contributions": 3},
                          {"login": "Copilot", "contributions": 2},
                          {"login": "dependabot[bot]", "contributions": 1}])
        return base(url, **kw)
    cs = collaborations("okmomnyi", ["Techtoxic/ums"], http=with_bot)
    assert cs[0].teammates == ["Techtoxic"], cs[0].teammates
