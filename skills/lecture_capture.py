"""Lecture capture pipeline (Phase 10).

Audio dropped in data/lectures/inbox/ (or sent to Telegram), tagged with a unit code, is
transcribed (faster-whisper, droplet CPU) → cleaned (filler removal, Swahili/English
code-switch fixes) → turned into: a full transcript (kept in the vault), structured
summary notes, an "examinable signals" list (things the lecturer flagged for the exam),
and 10–20 candidate flashcards (for Phase 11). Notes are delivered as a charcoal PDF and
the transcript is auto-ingested into the Study Vault under the unit. Idempotent: processed
audio is moved to data/lectures/processed/ and never re-run.

The transcriber is injectable so the pipeline is testable offline; real STT runs droplet-side.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Callable

from core.config import get_settings
from core.llm import LLMClient, LLMError, get_client
from core.logging_setup import get_logger
from core.memory import Memory, get_memory
from core.notify import send_telegram
from core.pdf import build_pdf
from core.transcribe import Transcriber, transcribe_audio
from core.skill import BaseSkill, CommandResult, ScheduledJob

log = get_logger("skills.lecture_capture")

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus"}
_NOTES_SCHEMA = ('{"title": string, "summary_notes": string (headings/definitions/examples), '
                 '"definitions": [{"term": string, "definition": string}], '
                 '"examinable_signals": [string], '
                 '"flashcards": [{"front": string, "back": string}]}')


class LectureCaptureSkill(BaseSkill):
    name = "lecture_capture"

    def __init__(self, memory: Memory | None = None, llm: LLMClient | None = None,
                 transcriber: Transcriber | None = None, vault: Any | None = None,
                 notify: Callable[[str], bool] | None = None) -> None:
        self._mem = memory
        self._llm = llm
        self._transcriber = transcriber
        self._vault = vault
        # Injectable so a test can never reach Calvin's phone. It wasn't, and process_inbox()
        # notifies unconditionally, so every suite run announced a "CS301: Binary Search Trees"
        # lecture he never recorded.
        self._notify = notify or send_telegram

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
    def transcriber(self) -> Transcriber:
        return self._transcriber or transcribe_audio

    @property
    def vault(self):
        if self._vault is None:
            from skills.study_vault import SKILL as vault_skill

            self._vault = vault_skill
        return self._vault

    @property
    def lectures_dir(self) -> Path:
        return get_settings().data_dir / "lectures"

    def commands(self) -> dict[str, Callable[..., CommandResult]]:
        return {"capture": self.capture, "process_inbox": self.process_inbox}

    def scheduled_jobs(self) -> list[ScheduledJob]:
        return [ScheduledJob(id="lecture.inbox", func=self.process_inbox, trigger="interval",
                             kwargs={"minutes": 10})]

    # ------------------------------------------------------------- inbox
    def process_inbox(self, **_: Any) -> CommandResult:
        """Process every audio file in data/lectures/inbox/. Unit code from '<UNIT>__name.ext'."""
        inbox = self.lectures_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        done = 0
        for f in sorted(inbox.iterdir()):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                unit = f.name.split("__")[0] if "__" in f.name else "UNSORTED"
                self.capture(path=str(f), unit=unit)
                done += 1
        return CommandResult(text=f"Processed {done} lecture(s) from the inbox.", data={"processed": done})

    # ------------------------------------------------------------- capture
    def capture(self, path: str = "", unit: str = "", notify: bool = True, **_: Any) -> CommandResult:
        """Full pipeline for one audio file. Returns paths + counts."""
        audio = Path(path)
        if not audio.exists():
            return CommandResult(text=f"Audio not found: {path}", ok=False)
        unit = unit or "UNSORTED"

        raw = self.transcriber(str(audio))
        if not raw.strip():
            return CommandResult(text="Transcription produced no text.", ok=False)

        clean = self._cleanup(raw)
        notes = self._notes(clean)

        transcript_path = self._save_transcript(unit, audio.stem, clean)
        cards_added = self._save_flashcards(unit, notes.get("flashcards", []), audio.name)
        pdf_path = self._write_pdf(unit, audio.stem, notes)
        ingest = self._ingest_to_vault(unit)
        self._archive(audio)

        summary = self._telegram_summary(unit, notes, pdf_path, cards_added)
        if notify:
            self._notify(summary)
        return CommandResult(
            text=summary,
            data={"unit": unit, "transcript": str(transcript_path), "pdf": str(pdf_path),
                  "flashcards": cards_added, "signals": len(notes.get("examinable_signals", [])),
                  "vault_ingested": ingest})

    def _cleanup(self, raw: str) -> str:
        try:
            return self.llm.chat(
                "write",
                [{"role": "system", "content":
                    "Clean this lecture transcript: remove filler words and false starts, fix obvious "
                    "ASR errors, and smooth Swahili/English code-switching artifacts — but do NOT summarize "
                    "or drop content. Return the cleaned transcript only."},
                 {"role": "user", "content": raw[:12000]}],
                max_tokens=4000).strip() or raw
        except LLMError:
            log.warning("cleanup pass failed — using raw transcript")
            return raw

    def _notes(self, clean: str) -> dict[str, Any]:
        try:
            return self.llm.chat_json(
                "write",
                [{"role": "system", "content":
                    "From this lecture transcript produce study materials: structured summary notes "
                    "(headings, definitions, worked examples), a list of 'examinable signals' — anything "
                    "the lecturer flagged as important/'this will be in the exam'/repeated definitions — "
                    "and 10-20 flashcards. Do NOT invent content not in the transcript. Return JSON."},
                 {"role": "user", "content": clean[:12000]}],
                schema_hint=_NOTES_SCHEMA, temperature=0.3, max_tokens=2500)
        except LLMError:
            log.exception("notes generation failed")
            return {"title": "Lecture", "summary_notes": clean[:2000], "definitions": [],
                    "examinable_signals": [], "flashcards": []}

    def _save_transcript(self, unit: str, stem: str, clean: str) -> Path:
        vault_unit = get_settings().data_dir / "vault" / unit
        vault_unit.mkdir(parents=True, exist_ok=True)
        out = vault_unit / f"{stem}_transcript.txt"
        out.write_text(clean, encoding="utf-8")
        return out

    def _save_flashcards(self, unit: str, cards: list[dict[str, str]], source_file: str) -> int:
        added = 0
        for c in cards:
            front, back = (c.get("front") or "").strip(), (c.get("back") or "").strip()
            if front and back and self.mem.add_flashcard(
                    front, back, unit=unit, source=f"lecture:{source_file}", status="candidate"):
                added += 1
        return added

    def _write_pdf(self, unit: str, stem: str, notes: dict[str, Any]) -> Path:
        out = self.lectures_dir / "notes" / f"{unit}_{stem}.pdf"
        defs = [f"- {d.get('term','')}: {d.get('definition','')}" for d in notes.get("definitions", [])]
        cards = [f"- Q: {c.get('front','')}  A: {c.get('back','')}" for c in notes.get("flashcards", [])]
        sections = [
            ("Summary notes", [notes.get("summary_notes", "")]),
            ("Definitions", defs or ["(none)"]),
            ("⚑ Examinable signals", [f"- {s}" for s in notes.get("examinable_signals", [])] or ["(none)"]),
            ("Flashcards", cards or ["(none)"]),
        ]
        title = notes.get("title") or f"{unit} — Lecture Notes"
        return build_pdf(out, title, sections, subtitle=f"{unit} · {time.strftime('%d %b %Y')}")

    def _ingest_to_vault(self, unit: str) -> int:
        try:
            return self.vault.ingest(unit=unit).data.get("chunks", 0)
        except Exception:  # noqa: BLE001
            log.exception("vault auto-ingest failed for unit %s", unit)
            return 0

    def _archive(self, audio: Path) -> None:
        processed = self.lectures_dir / "processed"
        processed.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(audio), str(processed / audio.name))  # move, not delete (§0)
        except Exception:  # noqa: BLE001
            log.warning("could not archive %s", audio)

    def _telegram_summary(self, unit: str, notes: dict[str, Any], pdf: Path, cards: int) -> str:
        signals = notes.get("examinable_signals", [])
        lines = [f"🎓 Lecture processed — {unit}: {notes.get('title', '')}",
                 f"{cards} flashcard(s) queued for review, {len(signals)} examinable signal(s)."]
        if signals:
            lines.append("⚑ Flagged for the exam:")
            lines.extend(f"  • {s}" for s in signals[:5])
        lines.append(f"📄 Notes PDF: {pdf.name}  ·  transcript added to your vault.")
        return "\n".join(lines)


SKILL = LectureCaptureSkill()
