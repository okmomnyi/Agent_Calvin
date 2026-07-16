"""CV tailoring tests: ATS scoring/keywords/fabrication check, master ingest + diff +
persona cross-check, and tailoring that NEVER adds unverified facts (flags gaps instead)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ats import ats_score, fabrication_terms, keywords, missing_keywords
from core.llm import LLMClient
from skills.cv_tailor import CvTailorSkill


# ------------------------------------------------------------------ ATS (pure)
def test_keywords_weights_tech_terms():
    kw = keywords("We need a DevOps engineer with Docker and Kubernetes. Docker is essential.")
    assert "docker" in kw and "kubernetes" in kw
    assert "the" not in kw  # stopword removed


def test_ats_score_and_missing():
    jd_kw = ["docker", "kubernetes", "terraform"]
    cv = "Experienced with Docker and Terraform on DigitalOcean."
    assert ats_score(cv, jd_kw) == 67          # 2 of 3
    assert missing_keywords(cv, jd_kw) == ["kubernetes"]


def test_fabrication_terms_detects_unsupported():
    master = "Skills: Docker, PM2, Caddy, Nginx."
    tailored = "Skills: Docker, Kubernetes, PM2."     # kubernetes was never in the master
    assert fabrication_terms(tailored, master) == ["kubernetes"]
    assert fabrication_terms("Docker, PM2", master) == []


# ------------------------------------------------------------------ skill fixture
class _CvLLM(LLMClient):
    def __init__(self, facts=None, tailor=None):
        self.routes = {"default": "m", "write": "m"}
        self.defaults = {}
        self._facts = facts
        self._tailor = tailor

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        blob = " ".join(m["content"] for m in messages).lower()
        if "parse this cv" in blob:
            return {"facts": self._facts or []}
        return self._tailor or {}


@pytest.fixture
def cv(mem, tmp_path, monkeypatch):
    import skills.cv_tailor as ct

    real = ct.get_settings()

    class _S:
        def __init__(self): self.data_dir = tmp_path
        def __getattr__(self, n): return getattr(real, n)

    monkeypatch.setattr(ct, "get_settings", lambda: _S())
    return CvTailorSkill(memory=mem, llm=None, clock=lambda: 1_000.0), tmp_path


# ------------------------------------------------------------------ update / ingest
def test_update_parses_master_and_reports_diff(cv, mem):
    skill, tmp = cv
    cvdir = tmp / "cv"; cvdir.mkdir()
    (cvdir / "master_cv.md").write_text(
        "# Calvin\nSkills: Docker, PM2, Caddy\nExperience: Full-stack dev", encoding="utf-8")
    skill._llm = _CvLLM(facts=[
        {"section": "skills", "key": "devops", "value": "Docker, PM2, Caddy, Nginx"},
        {"section": "experience", "key": "role1", "value": "Full-stack developer"}])
    res = skill.update()
    assert res.data["diff"]["added"]           # first ingest -> all added
    assert mem.execute("SELECT COUNT(*) c FROM cv_facts").fetchone()["c"] == 2
    assert mem.kv_get("cv.version")


def test_update_persona_crosscheck_flags_gap(cv, mem):
    skill, tmp = cv
    cvdir = tmp / "cv"; cvdir.mkdir()
    (cvdir / "master_cv.md").write_text("Skills: Docker", encoding="utf-8")
    # a verified persona skill that the CV won't mention
    mem.upsert_fact("skills", "kubernetes", "used on a project", verified=True)
    skill._llm = _CvLLM(facts=[{"section": "skills", "key": "devops", "value": "Docker, PM2"}])
    res = skill.update()
    assert any("kubernetes" in c for c in res.data["crosscheck"])


# ------------------------------------------------------------------ tailoring guardrail
def test_tailor_never_adds_unverified_flags_gap(cv, mem):
    skill, tmp = cv
    mem.replace_cv_facts([{"section": "skills", "key": "devops", "value": "Docker, PM2, Caddy, Nginx"}], "v1")
    jd = "DevOps role: Docker, Kubernetes and Terraform required."
    # a (bad) draft that tries to sneak in Kubernetes — the fabrication check must catch it
    skill._llm = _CvLLM(tailor={
        "cv_markdown": "## Skills\nDocker, PM2, Caddy, Nginx, Kubernetes\n## Summary\nDevOps-focused dev.",
        "changelog": ["Emphasized Docker/DevOps to match the listing"],
        "gaps": ["Kubernetes — not in your experience"]})
    res = skill.tailor(target=jd, company="Acme")
    assert "kubernetes" in res.data["fabricated"]           # unsupported term flagged
    assert any("Kubernetes" in g for g in res.data["gaps"]) # gap surfaced, not silently added
    assert Path(res.data["variant"]).exists()               # variant saved (never touches master)
    assert res.data["ats_after"] >= res.data["ats_before"]


def test_tailor_links_variant_to_job(cv, mem):
    import json

    skill, tmp = cv
    mem.replace_cv_facts([{"section": "skills", "key": "devops", "value": "Docker, Terraform"}], "v1")
    mem.upsert_job("remoteok", "j9", title="DevOps Engineer", company="Nimbus",
                   raw_json=json.dumps({"description": "Docker and Terraform on AWS."}))
    jid = mem.get_job_by_ref("remoteok", "j9")["id"]
    skill._llm = _CvLLM(tailor={"cv_markdown": "## Skills\nDocker, Terraform (AWS).",
                                "changelog": ["Reordered cloud skills first"], "gaps": []})
    skill.tailor(job_id=jid)
    variant = mem.get_job(jid)["cv_variant"]
    assert variant and Path(variant).exists()               # hunter will attach this on approval


def test_tailor_requires_cv_facts(cv, mem):
    skill, _ = cv
    res = skill.tailor(target="some JD")
    assert res.ok is False and "master CV" in res.text
