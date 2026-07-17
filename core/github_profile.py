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


class GitHubError(RuntimeError):
    pass


@dataclass
class Repo:
    name: str
    language: str | None
    description: str | None
    topics: list[str] = field(default_factory=list)
    fork: bool = False
    stars: int = 0
    pushed_at: str = ""
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
             stars=r.get("stargazers_count", 0), pushed_at=r.get("pushed_at", ""))
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
