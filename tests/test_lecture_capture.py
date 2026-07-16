"""Lecture capture tests: full pipeline outputs, candidate flashcards, PDF, vault
auto-ingest, and inbox idempotency (processed audio archived, not re-run). Offline."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.embeddings import HashingEmbedder
from core.llm import LLMClient
from skills.lecture_capture import LectureCaptureSkill
from skills.study_vault import VaultSkill


class _LectureLLM(LLMClient):
    """cleanup returns cleaned text; chat_json returns notes+signals+flashcards."""

    def __init__(self):
        self.routes = {"default": "m", "write": "m", "research": "m"}
        self.defaults = {}

    def chat(self, task, messages, **kw):  # type: ignore[override]
        return "Cleaned transcript about binary search trees and their O(log n) operations."

    def chat_json(self, task, messages, schema_hint, **kw):  # type: ignore[override]
        return {
            "title": "Binary Search Trees",
            "summary_notes": "A BST orders keys so lookup is O(log n) when balanced.",
            "definitions": [{"term": "BST", "definition": "ordered binary tree"}],
            "examinable_signals": ["Know the O(log n) proof for balanced trees",
                                   "Rotations will be on the exam"],
            "flashcards": [{"front": "BST lookup complexity?", "back": "O(log n) when balanced"},
                           {"front": "What keeps a BST balanced?", "back": "rotations (AVL/red-black)"}],
        }


@pytest.fixture
def lecture(mem, tmp_path, monkeypatch):
    import skills.lecture_capture as lc
    import skills.study_vault as sv

    real = lc.get_settings()

    class _S:
        def __init__(self): self.data_dir = tmp_path
        def __getattr__(self, n): return getattr(real, n)

    monkeypatch.setattr(lc, "get_settings", lambda: _S())
    monkeypatch.setattr(sv, "get_settings", lambda: _S())

    vault = VaultSkill(memory=mem, embedder=HashingEmbedder(), llm=_LectureLLM())
    skill = LectureCaptureSkill(memory=mem, llm=_LectureLLM(),
                                transcriber=lambda p: "um so uh binary search trees give log n lookup",
                                vault=vault)
    return skill, tmp_path


def test_capture_produces_all_outputs(lecture):
    skill, tmp = lecture
    audio = tmp / "lecture1.wav"
    audio.write_bytes(b"fake audio")
    result = skill.capture(path=str(audio), unit="CS301", notify=False)
    assert result.ok
    d = result.data
    # transcript kept in the vault
    assert Path(d["transcript"]).exists()
    assert "CS301" in d["transcript"]
    # notes PDF written
    assert Path(d["pdf"]).exists() and Path(d["pdf"]).stat().st_size > 800
    # flashcards + examinable signals
    assert d["flashcards"] == 2
    assert d["signals"] == 2
    # transcript auto-ingested into the vault
    assert d["vault_ingested"] >= 1


def test_flashcards_stored_as_candidates(lecture):
    skill, tmp = lecture
    audio = tmp / "lec.wav"; audio.write_bytes(b"x")
    skill.capture(path=str(audio), unit="CS301", notify=False)
    assert skill.mem.count_flashcards(unit="CS301", status="candidate") == 2
    assert skill.mem.count_flashcards(unit="CS301", status="active") == 0  # not active until Phase 11 approval


def test_captured_lecture_is_retrievable_from_vault(lecture):
    skill, tmp = lecture
    audio = tmp / "lec.wav"; audio.write_bytes(b"x")
    skill.capture(path=str(audio), unit="CS301", notify=False)
    # the cleaned transcript is now indexed under the unit and retrievable
    hits = skill.vault.retrieve("binary search trees O(log n) operations", unit="CS301")
    assert hits and "transcript" in hits[0]["file"]


def test_inbox_idempotent_archives_audio(lecture):
    skill, tmp = lecture
    inbox = tmp / "lectures" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "CS301__week1.wav").write_bytes(b"audio")
    first = skill.process_inbox()
    assert first.data["processed"] == 1
    # audio moved to processed/, so a second run finds nothing
    assert (tmp / "lectures" / "processed" / "CS301__week1.wav").exists()
    assert not (inbox / "CS301__week1.wav").exists()
    second = skill.process_inbox()
    assert second.data["processed"] == 0


def test_capture_missing_audio_is_graceful(lecture):
    skill, _ = lecture
    result = skill.capture(path="/nope/missing.wav", unit="CS301")
    assert result.ok is False
