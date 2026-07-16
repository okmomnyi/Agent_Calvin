"""Memory layer tests: idempotency, status transitions, never-delete guarantees."""

from __future__ import annotations

from pathlib import Path


def test_upsert_job_is_idempotent(mem):
    first = mem.upsert_job("remoteok", "abc123", url="http://x", title="DevOps Eng")
    second = mem.upsert_job("remoteok", "abc123", url="http://x", title="DevOps Eng")
    assert first is True   # first sighting
    assert second is False  # re-seen, not duplicated

    rows = mem.execute("SELECT COUNT(*) c FROM jobs").fetchone()
    assert rows["c"] == 1


def test_job_status_transition(mem):
    mem.upsert_job("remotive", "j1", title="SRE")
    job = mem.execute("SELECT * FROM jobs WHERE external_id='j1'").fetchone()
    assert job["status"] == "new"
    mem.score_job(job["id"], 72, category="cloud_devops", summary="good fit")
    scored = mem.get_job(job["id"])
    assert scored["status"] == "scored"
    assert scored["score"] == 72
    assert scored["category"] == "cloud_devops"


def test_email_recorded_once(mem):
    assert mem.record_email("gmail-1", subject="Hi", category="important") is True
    assert mem.record_email("gmail-1", subject="Hi", category="important") is False
    assert mem.email_seen("gmail-1") is True
    assert mem.email_seen("nope") is False


def test_persona_fact_upsert_updates_in_place(mem):
    mem.upsert_fact("skills", "docker", "1 year", confidence=0.6)
    mem.upsert_fact("skills", "docker", "2 years", confidence=0.9, verified=True)
    rows = mem.facts_by_category("skills")
    assert len(rows) == 1
    assert rows[0]["value"] == "2 years"
    assert rows[0]["verified"] == 1


def test_standing_instruction_soft_delete_never_deletes(mem):
    mem.add_instruction("don't message me before 8am")
    assert len(mem.list_instructions()) == 1
    mem.deactivate_instruction("don't message me before 8am")
    assert len(mem.list_instructions(active_only=True)) == 0
    # row still exists — soft delete only
    assert len(mem.list_instructions(active_only=False)) == 1


def test_kv_roundtrip(mem):
    mem.kv_set("voice", "zuri")
    assert mem.kv_get("voice") == "zuri"
    mem.kv_set("voice", "aria")  # overwrite
    assert mem.kv_get("voice") == "aria"
    assert mem.kv_get("missing", "fallback") == "fallback"


def test_event_upsert_idempotent(mem):
    assert mem.upsert_event("ctftime", "e1", title="Some CTF", fmt="online", free=True) is True
    assert mem.upsert_event("ctftime", "e1", title="Some CTF", fmt="online", free=True) is False


def test_cv_refresh_soft_deactivates_and_preserves_versions(mem):
    mem.replace_cv_facts([
        {"section": "skills", "key": "devops", "value": "Docker"},
        {"section": "skills", "key": "linux", "value": "Ubuntu"},
    ], "v1")
    mem.replace_cv_facts([
        {"section": "skills", "key": "devops", "value": "Docker, Terraform"},
    ], "v2")

    current = mem.get_cv_facts()
    assert [(r["key"], r["value"]) for r in current] == [
        ("devops", "Docker, Terraform")
    ]
    assert mem.execute("SELECT active FROM cv_facts WHERE key='linux'").fetchone()["active"] == 0
    assert mem.execute("SELECT COUNT(*) c FROM cv_fact_history").fetchone()["c"] == 3


def test_vault_refresh_soft_deactivates_and_preserves_versions(mem):
    def chunk(index, text):
        return {"loc": f"p.{index + 1}", "chunk_index": index, "text": text,
                "embedding": b"\x00\x00\x00\x00", "dim": 1}

    mem.replace_vault_file("CSC", "notes.txt", "hash-v1", [chunk(0, "old"), chunk(1, "stale")])
    mem.replace_vault_file("CSC", "notes.txt", "hash-v2", [chunk(0, "new")])

    current = mem.vault_chunks("CSC")
    assert len(current) == 1 and current[0]["text"] == "new"
    stale = mem.execute(
        "SELECT active FROM vault_chunks WHERE unit='CSC' AND file='notes.txt' AND chunk_index=1"
    ).fetchone()
    assert stale["active"] == 0
    assert mem.execute("SELECT COUNT(*) c FROM vault_chunk_history").fetchone()["c"] == 3


def test_memory_layer_contains_no_destructive_sql():
    source = (Path(__file__).parents[1] / "core" / "memory.py").read_text(encoding="utf-8")
    assert "delete from" not in source.lower()
