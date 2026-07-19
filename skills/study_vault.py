"""Study Vault — RAG over Calvin's course materials (Phase 9).

Calvin drops PDFs/PPTX/DOCX/note-images into data/vault/<unit_code>/. Ingestion extracts
text (OCR for images), chunks ~800 tokens, embeds locally (free/CPU), and stores vectors
in SQLite. vault.ask(question, unit) retrieves the top-k chunks and answers, ALWAYS citing
file + page/slide. When retrieval is weak it says "not in your notes" and can offer a
clearly-labelled web answer instead of guessing. Content is LOCAL: only the retrieved
chunks are ever sent to the NIM API to compose an answer (§0 integration note).

Design rule (§0): the vault is a tutor/organizer — it explains and cites, it does not
ghost-write assignments.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from core.config import get_settings
from core.doc_extract import SUPPORTED, chunk_passages, extract
from core.embeddings import Embedder, cosine, get_embedder, pack_vector, unpack_vector
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract

log = get_logger("skills.study_vault")


class VaultSkill(BaseSkill):
    name = "vault"

    def __init__(self, memory: Memory | None = None, embedder: Embedder | None = None,
                 llm: LLMClient | None = None, research: Any | None = None) -> None:
        self._mem = memory
        self._embedder = embedder
        self._llm = llm
        self._research = research

    @property
    def mem(self) -> Memory:
        if self._mem is None:
            self._mem = get_memory()
        return self._mem

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = get_client()
        return self._llm

    @property
    def research(self):
        if self._research is None:
            from skills.research import ResearchSkill

            self._research = ResearchSkill()
        return self._research

    @property
    def vault_dir(self) -> Path:
        return get_settings().data_dir / "vault"

    @property
    def min_score(self) -> float:
        return float(get_settings().get("vault", "min_score", default=0.18))

    def contract(self) -> SkillContract:
        """Reads `study` only — a tone rule must not reach a cited answer about his notes.

        `always_cites_source` and `never_answers_beyond_the_notes` are what make the vault
        trustworthy: below the score floor it says the answer is not in his notes and offers a
        clearly-labelled web answer instead of quietly blending the two.
        """
        return SkillContract(reads_categories=["study"],
                             hard_invariants=["always_cites_source",
                                              "never_answers_beyond_the_notes"])

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"ingest": self.ingest, "ask": self.ask, "status": self.status}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [ScheduledJob(id="vault.ingest", func=self.ingest, trigger="interval",
                             kwargs={"minutes": 10},
                         queued=True, skill="vault", action="ingest")]

    # ------------------------------------------------------------- ingest
    def ingest(self, unit: str = "", **_: Any) -> CommandResult:
        """Scan data/vault/<unit>/ (or all units) and (re)ingest new/changed files. Idempotent."""
        base = self.vault_dir
        if not base.exists():
            base.mkdir(parents=True, exist_ok=True)
            return CommandResult(text=f"Vault folder created at {base}. Drop notes into <unit>/ subfolders.",
                                 data={"ingested": 0})
        unit_dirs = [base / unit] if unit else [d for d in base.iterdir() if d.is_dir()]
        ingested, skipped, chunks_added = 0, 0, 0
        for udir in unit_dirs:
            if not udir.is_dir():
                continue
            ucode = udir.name
            for f in sorted(udir.iterdir()):
                if not f.is_file() or f.suffix.lower() not in SUPPORTED:
                    continue
                digest = _file_hash(f)
                if self.mem.vault_file_hash(ucode, f.name) == digest:
                    skipped += 1
                    continue
                chunks = self._embed_file(f)
                self.mem.replace_vault_file(ucode, f.name, digest, chunks)
                ingested += 1
                chunks_added += len(chunks)
                log.info("ingested %s/%s (%d chunks)", ucode, f.name, len(chunks))
        return CommandResult(
            text=f"Ingest done. {ingested} file(s) indexed ({chunks_added} chunks), {skipped} unchanged.",
            data={"ingested": ingested, "skipped": skipped, "chunks": chunks_added,
                  "embedder": self.embedder.name})

    def _embed_file(self, path: Path) -> list[dict[str, Any]]:
        passages = chunk_passages(extract(path))
        if not passages:
            return []
        vectors = self.embedder.embed_batch([p.text for p in passages])
        return [{"loc": p.loc, "chunk_index": i, "text": p.text,
                 "embedding": pack_vector(v), "dim": len(v)}
                for i, (p, v) in enumerate(zip(passages, vectors))]

    # ------------------------------------------------------------- ask
    def retrieve(self, question: str, unit: str | None = None, k: int = 5) -> list[dict[str, Any]]:
        qvec = self.embedder.embed(question)
        scored = []
        for row in self.mem.vault_chunks(unit):
            score = cosine(qvec, unpack_vector(row["embedding"]))
            scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"score": s, "file": r["file"], "loc": r["loc"], "unit": r["unit"],
                 "text": r["text"]} for s, r in scored[:k]]

    def ask(self, question: str = "", unit: str = "", allow_web: bool = True, **_: Any) -> CommandResult:
        """Answer from the notes with citations; fall back to 'not in your notes' (+ optional web)."""
        if not question.strip():
            return CommandResult(text="Ask me something about your notes.", ok=False)
        hits = self.retrieve(question, unit or None)
        best = hits[0]["score"] if hits else 0.0
        if not hits or best < self.min_score:
            return self._not_in_notes(question, allow_web)

        context = "\n\n".join(
            f"[{i+1}] {h['file']}" + (f" ({h['loc']})" if h['loc'] else "") + f":\n{h['text'][:1200]}"
            for i, h in enumerate(hits))
        try:
            answer = self.llm.chat(
                "research",
                [{"role": "system", "content":
                    "Answer the question using ONLY the student's course-note excerpts. Cite each claim "
                    "inline as [n]. If the excerpts don't answer it, say so plainly. Do not add outside "
                    "facts. You are a tutor: explain, don't write the student's assignment for them."},
                 {"role": "user", "content": f"QUESTION: {question}\n\nNOTE EXCERPTS:\n{context}"}],
                max_tokens=600)
        except LLMError:
            answer = "Found relevant notes but couldn't compose an answer right now."
        refs = "\n".join(
            f"[{i+1}] {h['file']}" + (f" ({h['loc']})" if h['loc'] else "") for i, h in enumerate(hits))
        return CommandResult(text=f"{answer.strip()}\n\nFrom your notes:\n{refs}",
                             data={"sources": hits, "best_score": round(best, 3)})

    def _not_in_notes(self, question: str, allow_web: bool) -> CommandResult:
        msg = "That's not in your notes."
        if not allow_web:
            return CommandResult(text=msg, data={"in_notes": False})
        try:
            web = self.research.research(question)
            if web.sources:
                return CommandResult(
                    text=f"{msg} Here's a web answer instead (⚠️ OUTSIDE your course materials):\n\n"
                         f"{web.cited_text()}",
                    data={"in_notes": False, "web": True})
        except Exception:  # noqa: BLE001
            log.exception("web fallback failed")
        return CommandResult(text=f"{msg} And I couldn't find a reliable web answer either.",
                             data={"in_notes": False})

    # ------------------------------------------------------------- status
    def status(self, **_: Any) -> CommandResult:
        st = self.mem.vault_status()
        if not st["units"]:
            return CommandResult(text=f"Vault empty. Drop notes into {self.vault_dir}/<unit>/ then /ingest.",
                                 data=st)
        lines = [f"📚 Vault — {st['total_chunks']} chunks across {len(st['units'])} unit(s) "
                 f"(embedder: {self.embedder.name}):"]
        for u in st["units"]:
            lines.append(f"  {u['unit']}: {u['files']} file(s), {u['chunks']} chunk(s)")
        return CommandResult(text="\n".join(lines), data=st)


def _file_hash(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


SKILL = VaultSkill()
