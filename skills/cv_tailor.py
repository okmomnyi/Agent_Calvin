"""CV tailoring & ATS optimization (Phase 15 — final).

Keeps one verified master CV (data/cv/master_cv.<ext>) as the source of truth. Ingesting
it parses structured cv_facts (shown as a diff for confirmation and cross-checked against
persona_facts). For any job, tailor() produces an ATS-optimized variant that ONLY reorders,
emphasizes, or rephrases what is already true — it can NEVER add a skill/tool/employer/
qualification Calvin doesn't have; if the JD wants something he lacks, it flags the gap
instead (a fabrication check backs the prompt). Shows an ATS keyword-match score before and
after, saves the variant to data/cv/variants/ (never overwriting the master), and links it
to the job so the hunter attaches it on approval.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from core.ats import ats_score, fabrication_terms, keywords, missing_keywords
from core.config import get_settings
from core.doc_extract import extract
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.cv_tailor")

_CV_SCHEMA = ('{"facts": [{"section": one of '
              '["summary","experience","skills","education","projects","certs"], '
              '"key": string, "value": string}]}')
_TAILOR_SCHEMA = ('{"cv_markdown": string (ATS-safe plain markdown, standard headers, no tables/'
                  'columns/images), "changelog": [string], "gaps": [string]}')
_SESSION_KEY = "cv_tailor.session"


class CvTailorSkill(BaseSkill):
    name = "cv_tailor"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mem = memory
        self._llm = llm
        self._now = clock

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def cv_dir(self) -> Path:
        return get_settings().data_dir / "cv"

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {
            "update": self.update,
            "view": self.view,
            "tailor": self.tailor,
            "refine": self.refine,
            "continue_refinement": self.continue_refinement,
            "facts": self.facts,
        }

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return []

    # ------------------------------------------------------------- master CV
    def _master_path(self) -> Path | None:
        if not self.cv_dir.exists():
            return None
        for p in sorted(self.cv_dir.glob("master_cv.*")):
            return p
        return None

    def _master_text(self) -> str:
        p = self._master_path()
        if p:
            passages = extract(p)
            if passages:
                return "\n".join(x.text for x in passages)
        # fall back to assembling from stored cv_facts
        return self._facts_text()

    def _facts_text(self) -> str:
        """Verified facts the tailor may draw on: the master CV PLUS confirmed persona facts.

        The master CV is general, but Calvin's GitHub-derived skills (Docker, TypeScript,
        PostgreSQL, his deployed projects and collaborations) are ALSO verified -- so a DevOps
        JD can be matched honestly instead of the tailor being blind to skills he has proven.
        Only VERIFIED persona facts are included (§0 P5): a candidate never reaches a CV.
        """
        lines = [f"[{r['section']}] {r['key']}: {r['value']}" for r in self.mem.get_cv_facts()]
        try:
            from core.persona_store import get_engine

            for r in get_engine().get_facts(verified_only=True):
                if r["category"] in ("skills", "tools", "languages", "work_history", "education"):
                    lines.append(f"[{r['category']}] {r['key']}: {r['value']}")
        except Exception:  # noqa: BLE001 - persona facts are a bonus, never required
            pass
        return "\n".join(lines)

    def update(self, path: str = "", **_: Any) -> CommandResult:
        """Ingest the master CV → structured cv_facts; return a diff + persona cross-check."""
        src = Path(path) if path else self._master_path()
        if not src or not src.exists():
            return CommandResult(
                text=f"No master CV found. Drop it at {self.cv_dir}/master_cv.<pdf|docx|md> then run /cv update.",
                ok=False)
        passages = extract(src)
        text = "\n".join(p.text for p in passages)
        if not text.strip():
            return CommandResult(text="Couldn't extract text from that CV file.", ok=False)
        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Parse this CV into structured facts. Extract ONLY what is written — do not infer or "
                    "embellish. Sections: summary, experience, skills, education, projects, certs. Return JSON."},
                 {"role": "user", "content": text[:8000]}],
                schema_hint=_CV_SCHEMA, temperature=0.0, max_tokens=1800)
        except LLMError:
            return CommandResult(text="Couldn't parse the CV right now.", ok=False)

        version = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._now()))
        diff = self.mem.replace_cv_facts(data.get("facts", []), version)
        crosscheck = self._persona_crosscheck()
        parts = [f"📄 Master CV ingested (v{version}). "
                 f"{len(diff['added'])} added, {len(diff['changed'])} changed, {len(diff['removed'])} removed."]
        if diff["changed"]:
            parts.append("Changed: " + "; ".join(f"{c['key']}" for c in diff["changed"][:6]))
        if crosscheck:
            parts.append("⚠️ Cross-check vs your persona facts flagged: " + "; ".join(crosscheck[:5]))
        return CommandResult(text="\n".join(parts),
                             data={"diff": diff, "crosscheck": crosscheck, "version": version})

    def _persona_crosscheck(self) -> list[str]:
        """Flag skills/education present in persona but missing from the CV (and vice-versa)."""
        cv_blob = self._facts_text().lower()
        flags = []
        for r in self.mem.facts_by_category("skills"):
            if r["verified"] and r["value"].lower() not in cv_blob:
                flags.append(f"persona skill '{r['key']}' not reflected in CV")
        return flags

    def facts(self, **_: Any) -> CommandResult:
        rows = self.mem.get_cv_facts()
        if not rows:
            return CommandResult(text="No CV parsed yet. Run /cv update.")
        by_section: dict[str, list[str]] = {}
        for r in rows:
            by_section.setdefault(r["section"], []).append(f"{r['key']}: {r['value']}")
        lines = [f"CV facts (v{self.mem.kv_get('cv.version', '?')}):"]
        for sec, items in by_section.items():
            lines.append(f"\n[{sec}]")
            lines.extend(f"  • {i}" for i in items)
        return CommandResult(text="\n".join(lines), data={"count": len(rows)})

    def view(self, **_: Any) -> CommandResult:
        master = self._master_path()
        variants = sorted((self.cv_dir / "variants").glob("*")) if (self.cv_dir / "variants").exists() else []
        lines = [f"Master CV: {master.name if master else '(none — /cv update)'}",
                 f"CV facts: {len(self.mem.get_cv_facts())}",
                 f"Tailored variants: {len(variants)}"]
        lines.extend(f"  • {v.name}" for v in variants[-10:])
        return CommandResult(text="\n".join(lines), data={"variants": [str(v) for v in variants]})

    # ------------------------------------------------------------- tailoring
    def refine(self, target: str = "", **kwargs: Any) -> CommandResult:
        """Start a guided CV refinement instead of requiring a perfect one-shot command."""
        target = (target or "").strip()
        if target:
            return self.tailor(target=target, **kwargs)

        if not self.mem.get_cv_facts():
            if not self._master_path():
                return CommandResult(
                    text=(f"I need your master CV first. Put it at {self.cv_dir}/master_cv.pdf "
                          "(or .docx/.md), then say ‘refine my CV’ again."),
                    ok=False,
                )
            imported = self.update()
            if not imported.ok:
                return imported

        self.mem.kv_set(_SESSION_KEY, json.dumps({"stage": "awaiting_target"}))
        return CommandResult(
            text=("Your master CV is ready. Which role should I refine it for? "
                  "Paste or read the job description, or say ‘job 12’ for a saved job."),
            data={"awaiting": "job_description"},
        )

    def continue_refinement(self, text: str = "", **_: Any) -> CommandResult:
        """Consume the next conversational turn in an active refinement flow."""
        text = (text or "").strip()
        if not text:
            return CommandResult(text="Tell me the role or job description you want to target.", ok=False)
        if re.fullmatch(r"(?:cancel|stop|never\s*mind)", text, re.I):
            self.mem.kv_set(_SESSION_KEY, "")
            return CommandResult(text="CV refinement cancelled.")

        match = re.fullmatch(r"(?:saved\s+)?job(?:\s+(?:number|id))?\s*#?\s*(\d+)", text, re.I)
        if match:
            result = self.tailor(job_id=int(match.group(1)))
        else:
            result = self.tailor(target=text)
        if result.ok:
            self.mem.kv_set(_SESSION_KEY, "")
        return result

    def tailor(self, target: str = "", job_id: int | str = 0, company: str = "", **_: Any) -> CommandResult:
        """Tailor the CV to a job description (or a stored job). Never adds unverified facts."""
        jd, job_row = self._resolve_jd(target, job_id)
        if job_id and job_row is None:
            return CommandResult(text=f"Saved job {job_id} was not found.", ok=False)
        if not jd.strip():
            return self.refine()
        if not self.mem.get_cv_facts():
            return CommandResult(text="No CV on file. Run /cv update with your master CV first.", ok=False)

        master_text = self._master_text()
        jd_kw = keywords(jd)
        before = ats_score(master_text, jd_kw)
        gaps_pre = missing_keywords(master_text, jd_kw)

        try:
            data = self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "Tailor this candidate's CV to the job. HARD RULES: use ONLY the verified CV facts "
                    "provided; you may reorder, emphasize, and rephrase, and mirror the job's terminology "
                    "ONLY where it is genuinely true of the candidate. NEVER add a skill, tool, employer, "
                    "or qualification not in the facts — if the job wants something missing, list it in "
                    "'gaps' instead. ATS-safe plain markdown (standard section headers, no tables/columns/"
                    "images). Return JSON."},
                 {"role": "user", "content":
                    f"VERIFIED CV FACTS:\n{self._facts_text()}\n\nJOB DESCRIPTION:\n{jd[:3000]}"}],
                schema_hint=_TAILOR_SCHEMA, temperature=0.3, max_tokens=2000)
        except LLMError:
            return CommandResult(text="Couldn't tailor the CV right now.", ok=False)

        tailored = data.get("cv_markdown", "")
        # §0 anti-fabrication check: strip/flag any tech term not supported by the master CV.
        fabricated = fabrication_terms(tailored, master_text + " " + self._facts_text())
        after = ats_score(tailored, jd_kw)
        gaps = list(data.get("gaps", []))
        gap_blob = " ".join(str(g).lower() for g in gaps)
        gaps.extend(
            f"(keyword) {keyword}"
            for keyword in gaps_pre
            if keyword not in tailored.lower() and keyword not in gap_blob
        )

        # A "refinement" is not allowed to make the CV objectively worse or persist a draft
        # containing unsupported tech claims. Keep the verified master as the safe variant;
        # the attempted terms remain in the report so Calvin can confirm any that are true.
        safeguard_reason = ""
        if fabricated:
            safeguard_reason = "the draft introduced unsupported terms"
        elif after < before:
            safeguard_reason = f"the draft reduced ATS match from {before} to {after}"
        if safeguard_reason:
            tailored = master_text
            after = before
            data.setdefault("changelog", []).append(
                f"Safety fallback: preserved the verified master because {safeguard_reason}."
            )

        company = company or (job_row["company"] if job_row else "job")
        variant_path = self._save_variant(job_id, company, tailored)
        if job_row:
            self.mem.set_job_cv_variant(int(job_row["id"]), str(variant_path))

        lines = [f"📄 Tailored CV → {variant_path.name}",
                 f"ATS keyword match: {before} → {after} / 100",
                 "Changelog:"]
        lines.extend(f"  • {c}" for c in data.get("changelog", [])[:8])
        if gaps:
            lines.append("⚠️ Gaps (NOT added — only if genuinely true, tell me and I'll include them):")
            lines.extend(f"  • {g}" for g in gaps[:6])
        if fabricated:
            lines.append("🚫 Removed/flagged unsupported terms the draft tried to add: " + ", ".join(fabricated))
        return CommandResult(text="\n".join(lines),
                             data={"variant": str(variant_path), "ats_before": before, "ats_after": after,
                                   "gaps": gaps, "fabricated": fabricated,
                                   "safeguard": safeguard_reason})

    def _resolve_jd(self, target: str, job_id: int | str) -> tuple[str, Any]:
        if job_id:
            row = self.mem.get_job(int(job_id))
            if row:
                import json as _json

                raw = _json.loads(row["raw_json"]) if row["raw_json"] else {}
                jd = f"{row['title']}\n{raw.get('description', '')}"
                return jd, row
            return "", None
        return target, None

    def _save_variant(self, job_id: int | str, company: str, markdown: str) -> Path:
        """Write the variant and return the PDF -- the thing an employer actually receives.

        The markdown is kept alongside as the editable source, but the RETURNED path is the
        PDF, because this path is what gets attached to applications. It used to return the
        .md, so a real employer would have been sent a raw markdown file, and Calvin's own
        copies arrived as unformatted text pasted into an email body.

        Falls back to the markdown only if rendering genuinely fails -- an application should
        never be blocked, but it should also never silently go out as .md when a PDF was
        possible, so the failure is logged loudly.
        """
        vdir = self.cv_dir / "variants"
        vdir.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in str(company) if c.isalnum() or c in " -_").strip().replace(" ", "_") or "job"
        stamp = time.strftime("%Y%m%d", time.localtime(self._now()))
        stem = f"{job_id or 'jd'}_{safe}_{stamp}"
        md_out = vdir / f"{stem}.md"          # never overwrites master_cv.*
        md_out.write_text(markdown, encoding="utf-8")

        try:
            from core.cv_pdf import build_cv_pdf, extract_contact

            # _master_path(), not job_hunter.find_master_cv(): job_hunter imports this module,
            # so reaching back into it would be a circular import.
            master = self._master_path()
            contact = extract_contact(master) if master else None
            return build_cv_pdf(markdown, vdir / f"{stem}.pdf", contact)
        except Exception:  # noqa: BLE001
            log.exception("CV PDF rendering failed — falling back to markdown for %s", stem)
            return md_out


SKILL = CvTailorSkill()
