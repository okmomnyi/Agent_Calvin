"""CV tailoring tests: ATS scoring/keywords/fabrication check, master ingest + diff +
persona cross-check, and tailoring that NEVER adds unverified facts (flags gaps instead)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.ats import ats_score, fabrication_terms, keywords, missing_keywords
from core.llm import LLMClient
from kernel.registry import SkillRegistry
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
    cvdir = tmp / "cv"
    cvdir.mkdir()
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
    cvdir = tmp / "cv"
    cvdir.mkdir()
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
    variant = Path(res.data["variant"])
    assert variant.exists()                                 # variant saved (never touches master)
    assert variant.suffix == ".pdf"                         # what an employer receives
    assert res.data["ats_after"] >= res.data["ats_before"]
    # the .md sibling is the editable source; read that for content assertions
    saved = variant.with_suffix(".md").read_text(encoding="utf-8").lower()
    assert "kubernetes" not in saved


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


def test_refinement_is_a_guided_two_turn_conversation(cv, mem):
    skill, _ = cv
    mem.replace_cv_facts(
        [{"section": "skills", "key": "devops", "value": "Docker and Terraform"}], "v1")
    skill._llm = _CvLLM(tailor={
        "cv_markdown": "## Skills\nDocker and Terraform",
        "changelog": ["Focused the summary on DevOps"],
        "gaps": [],
    })

    started = skill.refine()
    assert started.ok is True and started.data["awaiting"] == "job_description"
    assert mem.kv_get("cv_tailor.session")

    finished = skill.continue_refinement(text="DevOps engineer using Docker and Terraform")
    assert finished.ok is True and finished.data["variant"]
    assert mem.kv_get("cv_tailor.session") == ""


def test_refinement_keeps_waiting_when_saved_job_does_not_exist(cv, mem):
    skill, _ = cv
    mem.replace_cv_facts([{"section": "skills", "key": "devops", "value": "Docker"}], "v1")
    skill.refine()

    result = skill.continue_refinement(text="job 999")
    assert result.ok is False and "not found" in result.text
    assert mem.kv_get("cv_tailor.session")


def test_registry_routes_plain_followup_into_active_cv_flow(cv, mem, monkeypatch):
    skill, _ = cv
    mem.replace_cv_facts([{"section": "skills", "key": "devops", "value": "Docker"}], "v1")
    skill._llm = _CvLLM(tailor={
        "cv_markdown": "## Skills\nDocker", "changelog": [], "gaps": []})
    monkeypatch.setattr("core.memory.get_memory", lambda: mem)
    registry = SkillRegistry()
    registry.register(skill)
    skill.refine()

    intent, result = registry.handle_command("Cloud role requiring Docker", use_llm=False)
    assert intent.via == "session"
    assert intent.action == "continue_refinement"
    assert result.ok is True


def test_tailor_never_saves_a_lower_scoring_variant(cv, mem):
    skill, _ = cv
    mem.replace_cv_facts(
        [{"section": "skills", "key": "cloud", "value": "Docker Linux Git CI/CD"}], "v1")
    skill._llm = _CvLLM(tailor={
        "cv_markdown": "## Profile\nCloud learner",
        "changelog": ["Shortened everything"],
        "gaps": [],
    })

    result = skill.tailor(target="Docker Linux Git CI/CD cloud engineer")
    assert result.data["ats_after"] == result.data["ats_before"]
    assert "reduced ATS match" in result.data["safeguard"]


def test_the_master_cv_file_is_never_modified_by_tailoring(cv, mem):
    """Calvin: "the mastercv should stay the same ... only be altered when i say so".

    Tailoring writes variants; the master is the source of truth and must come out of a tailor
    run byte-for-byte identical. Asserted on the FILE, not on intent -- an accidental
    write-back would be silent and would corrupt every future application.
    """
    import hashlib

    skill, tmp = cv
    master = tmp / "cv" / "master_cv.md"
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_text("# Calvin\n## Skills\nDocker, PM2, Caddy, Nginx\n", encoding="utf-8")
    before = hashlib.sha256(master.read_bytes()).hexdigest()
    mtime_before = master.stat().st_mtime

    mem.replace_cv_facts([{"section": "skills", "key": "devops", "value": "Docker, PM2, Caddy"}], "v1")
    skill._llm = _CvLLM(tailor={
        "cv_markdown": "## Skills\nDocker, PM2, Caddy, Nginx\n## Summary\nDevOps-focused.",
        "changelog": ["Reordered for the role"], "gaps": []})
    res = skill.tailor(target="DevOps role: Docker, Nginx.", company="Acme")

    assert res.ok
    assert hashlib.sha256(master.read_bytes()).hexdigest() == before, "master CV was rewritten"
    assert master.stat().st_mtime == mtime_before, "master CV was touched"
    # and the variant is a genuinely separate file
    assert Path(res.data["variant"]).resolve() != master.resolve()


# ============================================================ PDF extraction damage
def test_two_column_cv_extraction_is_repaired_before_parsing():
    """pypdf mashes a two-column CV's columns together and breaks words on kerning.

    Unrepaired, "Meru University of Science and T echnologyMeru, KE" followed by "Computer
    Pride Mombasa, KE" led the parser to record Calvin's DEGREE as a BSc from Computer Pride
    lasting June-July 2024 -- that was his CompTIA course. His BSc is Meru University,
    Sept 2024 - Apr 2028. Wrong education on a document employers read.
    """
    from core.doc_extract import clean_pdf_artifacts

    raw = ("Education\n"
           "Meru University of Science and T echnologyMeru, KE\n"
           "Bachelor of Science in Computer Science Sept. 2024 - Apr 2028\n"
           "Computer Pride Mombasa, KE\n"
           "CompTIA network+ June. 2024 - July 2024\n"
           "Experience\nIndependent Cybersecurity & Cloud Projects2024 - Present\n")
    out = clean_pdf_artifacts(raw)
    assert "Technology" in out and "T echnology" not in out      # kerning repaired
    assert "Technology  |  Meru, KE" in out                      # location separated
    assert "Projects  |  2024 - Present" in out                  # dates separated
    # the two institutions stay on their own lines, not merged
    assert "Meru University of Science and Technology" in out
    assert "Computer Pride" in out


def test_repair_does_not_invent_or_drop_words():
    """Only separators and kerning change -- never the words themselves (§0 P5)."""
    from core.doc_extract import clean_pdf_artifacts

    import re as _re

    raw = "Meru University of Science and T echnologyMeru, KE"
    out = clean_pdf_artifacts(raw)
    # Compare the letters themselves, not tokens: the repair deliberately changes word
    # BOUNDARIES (that is the fix), but must never add or lose a character.
    strip = lambda t: _re.sub(r"[^A-Za-z0-9]", "", t)
    assert strip(raw) == strip(out), "the repair changed the actual content"


def test_single_letter_words_are_not_glued():
    """'I am' and 'A test' must survive the kerning repair."""
    from core.doc_extract import clean_pdf_artifacts

    assert clean_pdf_artifacts("I applied to A team") == "I applied to A team"
