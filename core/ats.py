"""ATS (applicant tracking system) keyword matching for CV tailoring (Phase 15).

Pure functions: pull the meaningful keywords out of a job description, score how many
appear in a CV (0-100), and detect terms a tailored CV claims that the master CV does NOT
support — the anti-fabrication check that enforces the §0 hard rule (tailoring may only
reorder/emphasize what is already true, never add a skill/tool/qualification).
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9+#]+")   # keeps c++/c#; excludes '.' so trailing periods don't stick
_STOP = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have", "has", "this",
    "that", "who", "role", "team", "work", "working", "experience", "years", "year", "must",
    "should", "including", "etc", "able", "strong", "good", "plus", "job", "candidate", "we",
    "a", "an", "to", "of", "in", "on", "as", "is", "be", "or", "at", "by", "it", "using",
}
# Tech terms we care about detecting even as multi-char tokens (used for fabrication checks).
TECH_TERMS = {
    "docker", "kubernetes", "k8s", "terraform", "ansible", "jenkins", "gitlab", "github",
    "aws", "gcp", "azure", "linux", "nginx", "caddy", "cloudflare", "pm2", "ci/cd", "cicd",
    "python", "java", "c++", "cpp", "javascript", "typescript", "react", "node", "sql",
    "postgres", "mongodb", "redis", "prometheus", "grafana", "bash", "devops", "sre",
    "microservices", "kafka", "rabbitmq", "helm", "istio", "serverless", "lambda",
    "transcription", "subtitling", "captioning", "networking", "security",
}


def keywords(text: str, top: int = 40) -> list[str]:
    """Extract candidate ATS keywords from a job description (frequency-ranked, tech-weighted)."""
    tokens = [t for t in _WORD.findall((text or "").lower()) if t not in _STOP and len(t) > 2]
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    for t in list(freq):                       # weight tech terms so they rank/keep
        if t in TECH_TERMS:
            freq[t] += 3
    ranked = sorted(freq, key=lambda k: freq[k], reverse=True)
    return ranked[:top]


def ats_score(cv_text: str, jd_keywords: list[str]) -> int:
    """Percentage (0-100) of the JD keywords present in the CV text."""
    if not jd_keywords:
        return 0
    cv_tokens = set(_WORD.findall((cv_text or "").lower()))
    hit = sum(1 for k in jd_keywords if k in cv_tokens)
    return round(hit * 100 / len(jd_keywords))


def missing_keywords(cv_text: str, jd_keywords: list[str]) -> list[str]:
    """JD keywords NOT found in the CV — candidate gaps to flag (never auto-add)."""
    cv_tokens = set(_WORD.findall((cv_text or "").lower()))
    return [k for k in jd_keywords if k not in cv_tokens]


def fabrication_terms(tailored_text: str, master_text: str) -> list[str]:
    """Tech terms the tailored CV introduces that the master CV does not contain (§0 violation)."""
    master_tokens = set(_WORD.findall((master_text or "").lower()))
    tailored_tokens = set(_WORD.findall((tailored_text or "").lower()))
    return sorted(t for t in (tailored_tokens & TECH_TERMS) if t not in master_tokens)
