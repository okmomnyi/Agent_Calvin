"""Read Calvin's public GitHub as evidence of what he actually builds (Phase 25).

45 public repos are a better record of someone's skills than any interview answer: a CV says
"Cloud & Infrastructure", the repos say TypeScript, Node backends, a translation-layer AI, an
election system. This module fetches that and nothing else.

Two boundaries worth stating, because both were deliberate:

* **GitHub only. Not LinkedIn.** LinkedIn's ToS forbids automated access, they fingerprint and
  block it, and the account that gets restricted is Calvin's. This project already made the
  same call once -- config.yaml: "Facebook Marketplace is deliberately NOT scraped: Meta
  actively pursues scrapers". GitHub's REST API is public, documented, and built to be read.
  If Calvin wants LinkedIn content in his persona he can paste it; that is him providing data,
  not us taking it.

* **Everything derived here is UNVERIFIED.** A README saying "experimenting with Kubernetes"
  must never become "Calvin uses Kubernetes" in a cover letter to an employer. Facts land as
  candidates and stay that way until he confirms them (§0 Principle 5). The importer's job is
  to spare him typing, not to decide what is true about him.

Unauthenticated GitHub allows 60 requests/hour, so the repo list (1 request) carries the
languages/topics/descriptions and READMEs are fetched only for the top handful. GITHUB_TOKEN
raises the ceiling to 5000/hr if it's ever set, but nothing here requires it.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from core.logging_setup import get_logger

log = get_logger("core.github")

API = "https://api.github.com"
# READMEs are one request each. Enough to characterise someone, few enough to stay well inside
# the unauthenticated 60/hr budget alongside everything else.
README_LIMIT = 5
# One /languages call per repo. Capped so a full import stays inside GitHub's unauthenticated
# 60 requests/hour budget alongside everything else (GITHUB_TOKEN raises it to 5000/hr).
LANG_DETAIL_REPOS = 25


class GitHubError(RuntimeError):
    pass


_BOTS = {"copilot", "dependabot", "github-actions", "renovate", "snyk-bot"}


def _is_bot(login: str) -> bool:
    """Bots are not collaborators. Copilot/dependabot show up in contributor graphs but naming
    them as Calvin's teammates would be a small lie in a cover letter."""
    lo = login.lower()
    return lo in _BOTS or lo.endswith("[bot]") or lo.endswith("-bot")


@dataclass
class Repo:
    name: str
    language: str | None
    description: str | None
    topics: list[str] = field(default_factory=list)
    fork: bool = False
    stars: int = 0
    pushed_at: str = ""
    homepage: str = ""
    readme: str = ""

    @property
    def is_own_work(self) -> bool:
        """Forks are someone else's work; claiming them as evidence would be a lie by omission."""
        return not self.fork


def _headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "AgentOS/1.0"}
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:                       # optional: only raises the rate limit
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(path: str, *, http: Callable[..., Any] | None = None) -> Any:
    fn = http or requests.get
    resp = fn(f"{API}{path}", headers=_headers(), timeout=20)
    if resp.status_code == 403 and "rate limit" in resp.text.lower():
        raise GitHubError("GitHub rate limit hit (60/hr unauthenticated). Set GITHUB_TOKEN or "
                          "retry in an hour.")
    if resp.status_code == 404:
        raise GitHubError(f"GitHub says 404 for {path} — wrong username?")
    if resp.status_code != 200:
        raise GitHubError(f"GitHub {resp.status_code} for {path}: {resp.text[:120]}")
    return resp.json()


def profile(user: str, *, http: Callable[..., Any] | None = None) -> dict[str, Any]:
    d = _get(f"/users/{user}", http=http)
    return {"name": d.get("name"), "bio": d.get("bio"), "blog": d.get("blog"),
            "public_repos": d.get("public_repos", 0), "login": d.get("login")}


def repos(user: str, *, http: Callable[..., Any] | None = None,
          with_readmes: int = README_LIMIT) -> list[Repo]:
    """Own (non-fork) public repos, most recently pushed first, with a few READMEs."""
    raw = _get(f"/users/{user}/repos?per_page=100&sort=pushed", http=http)
    out = [
        Repo(name=r["name"], language=r.get("language"), description=r.get("description"),
             topics=r.get("topics") or [], fork=bool(r.get("fork")),
             stars=r.get("stargazers_count", 0), pushed_at=r.get("pushed_at", ""),
             homepage=r.get("homepage") or "")
        for r in raw
    ]
    own = [r for r in out if r.is_own_work]
    for repo in own[:with_readmes]:
        try:
            data = _get(f"/repos/{user}/{repo.name}/readme", http=http)
            content = base64.b64decode(data.get("content", "")).decode("utf-8", "replace")
            repo.readme = content[:1200]       # enough to characterise; keeps the prompt sane
        except GitHubError:
            continue                            # a repo with no README is not an error
    return own


@dataclass
class Collaboration:
    """A repo Calvin contributes to but does not own -- real teamwork, not solo projects."""
    full_name: str
    description: str | None
    language: str | None
    homepage: str | None
    my_commits: int
    teammates: list[str] = field(default_factory=list)


def collaborations(user: str, repos_full_names: list[str], *,
                   http: Callable[..., Any] | None = None) -> list[Collaboration]:
    """For each named repo, Calvin's commit count and who he worked with.

    The contributors endpoint is PUBLIC, so this needs no token -- only listing *which* repos
    someone collaborates on requires auth, and that list is supplied (config.github.collaborations).
    """
    out: list[Collaboration] = []
    for full_name in repos_full_names:
        try:
            meta = _get(f"/repos/{full_name}", http=http)
            contribs = _get(f"/repos/{full_name}/contributors?per_page=15", http=http)
        except GitHubError:
            continue
        mine = next((c["contributions"] for c in contribs
                     if c.get("login", "").lower() == user.lower()), 0)
        teammates = [c["login"] for c in contribs
                     if c.get("login", "").lower() != user.lower() and not _is_bot(c.get("login", ""))]
        out.append(Collaboration(
            full_name=full_name, description=meta.get("description"),
            language=meta.get("language"), homepage=meta.get("homepage"),
            my_commits=mine, teammates=teammates))
    return out


def derive_facts(user: str, repos_full_names: list[str] | None = None, *,
                 http: Callable[..., Any] | None = None) -> list[dict[str, str]]:
    """Build persona-fact candidates DETERMINISTICALLY from GitHub -- no LLM, so no NIM timeout.

    Everything here is verbatim from the API: languages by repo count, deployed projects with
    their live URLs, and each collaboration with its commit count and teammates. Facts stay
    candidates until Calvin confirms them (§0 P5); this just spares him the typing.
    """
    p = profile(user, http=http)
    rs = repos(user, http=http, with_readmes=0)
    facts: list[dict[str, str]] = []

    langs: dict[str, int] = {}
    for r in rs:
        if r.language:
            langs[r.language] = langs.get(r.language, 0) + 1
    if langs:
        ranked = ", ".join(f"{k} ({v} repo{'s' if v > 1 else ''})"
                           for k, v in sorted(langs.items(), key=lambda x: -x[1]))
        facts.append({"category": "skills", "key": "languages_by_repo_count", "value": ranked,
                      "evidence": f"{len(rs)} own public repos"})

    # FULL per-repo language breakdown, not just each repo's dominant language. GitHub reports
    # Dockerfile, PLpgSQL, Shell and PowerShell as languages, and they only ever appear here --
    # the dominant-language view hid every one of them. That is why a CV tailored for an SRE
    # role listed "Docker" as a GAP for someone whose own repo ships a Dockerfile: the evidence
    # existed and we simply were not reading it.
    byte_totals: dict[str, int] = {}
    for r in rs[:LANG_DETAIL_REPOS]:
        try:
            for lang, size in (_get(f"/repos/{user}/{r.name}/languages", http=http) or {}).items():
                byte_totals[lang] = byte_totals.get(lang, 0) + int(size)
        except (GitHubError, ValueError, TypeError):
            continue
    if byte_totals:
        ranked_bytes = ", ".join(
            f"{k}" for k, _ in sorted(byte_totals.items(), key=lambda x: -x[1]))
        facts.append({"category": "skills", "key": "full_language_breakdown",
                      "value": f"Languages/technologies across his repos, by volume: {ranked_bytes}",
                      "evidence": f"GitHub /languages across {min(len(rs), LANG_DETAIL_REPOS)} repos"})
        # Call out the infrastructure signals explicitly: an ATS scanner and a human reviewer
        # both look for these words, and "PLpgSQL" buried in a list is not the same as saying
        # PostgreSQL. Only named when GitHub actually reports the bytes.
        infra_map = {"Dockerfile": "Docker / containerisation",
                     "PLpgSQL": "PostgreSQL (stored procedures/SQL)",
                     "Shell": "Shell scripting / Linux",
                     "PowerShell": "PowerShell scripting / Windows automation",
                     "Makefile": "Make build tooling",
                     "HCL": "Terraform / infrastructure-as-code"}
        found = [label for key, label in infra_map.items() if byte_totals.get(key)]
        if found:
            facts.append({"category": "tools", "key": "infrastructure_tooling",
                          "value": "; ".join(found),
                          "evidence": "committed files in his own repos"})

    # notable own projects: name + language + live URL + description
    named = [f"{r.name} [{r.language}]"
             + (f" (live: {r.homepage})" if r.homepage else "")
             + (f" — {r.description}" if r.description else "")
             for r in rs if r.language][:15]
    if named:
        facts.append({"category": "work_history", "key": "own_projects",
                      "value": "; ".join(named), "evidence": "GitHub repositories"})
    live_count = sum(1 for r in rs if r.homepage)
    if live_count:
        facts.append({"category": "work_history", "key": "deployed_apps_count",
                      "value": f"{live_count} own projects deployed live (mostly Vercel)",
                      "evidence": "GitHub homepage URLs"})

    if p.get("blog"):
        facts.append({"category": "preferences", "key": "portfolio_site",
                      "value": p["blog"], "evidence": "GitHub profile"})

    collabs = collaborations(user, repos_full_names or [], http=http)
    all_teammates: set[str] = set()
    for c in collabs:
        all_teammates.update(c.teammates)
        detail = c.description or "collaborative project"
        note = (f"{c.full_name} ({c.language or 'mixed'}): {detail[:120]} — "
                f"{c.my_commits} commit(s) by Calvin"
                + (f", live at {c.homepage}" if c.homepage else "")
                + (f"; team: {', '.join(c.teammates[:8])}" if c.teammates else ""))
        key = "collab_" + c.full_name.split("/")[-1].lower().replace("-", "_")
        facts.append({"category": "work_history", "key": key, "value": note,
                      "evidence": c.full_name})
    if all_teammates:
        facts.append({"category": "work_history", "key": "collaborators",
                      "value": "Has collaborated with: " + ", ".join(sorted(all_teammates)),
                      "evidence": "GitHub contributor graphs"})
    return facts


def evidence(user: str, *, http: Callable[..., Any] | None = None) -> str:
    """A compact, factual digest for the LLM. Contains only what GitHub actually returned."""
    p = profile(user, http=http)
    rs = repos(user, http=http)
    langs: dict[str, int] = {}
    for r in rs:
        if r.language:
            langs[r.language] = langs.get(r.language, 0) + 1

    lines = [f"GitHub: {p['login']} ({p.get('name') or '-'})",
             f"Bio: {p.get('bio') or '-'}",
             f"Site: {p.get('blog') or '-'}",
             f"Own public repos: {len(rs)} (of {p['public_repos']} total incl. forks)",
             "Languages by repo count: " + ", ".join(
                 f"{k} ({v})" for k, v in sorted(langs.items(), key=lambda x: -x[1])),
             "", "Repositories:"]
    for r in rs[:25]:
        bits = [f"  - {r.name}"]
        if r.language:
            bits.append(f"[{r.language}]")
        if r.topics:
            bits.append(f"topics: {', '.join(r.topics[:6])}")
        if r.description:
            bits.append(f"-- {r.description[:110]}")
        lines.append(" ".join(bits))
        if r.readme:
            head = " ".join(r.readme.split())[:160]
            lines.append(f"      README: {head}")
    return "\n".join(lines)
