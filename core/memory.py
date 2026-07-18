"""PostgreSQL persistence layer for AgentOS.

One database (DATABASE_URL) holds all durable state: scraped jobs, processed emails,
applications, the persona knowledge base, standing instructions, CV facts, events, the
flip pipeline, an edit-log for the learning loop, and a generic kv store.

Raw SQL via psycopg 3 — no ORM (deliberate project convention). Postgres (rather than an
embedded file DB) matters here because the API process and the Telegram bot both write
concurrently; a real server handles that without the writer-lock contention a file DB hits.

Principles (§0): writes are idempotent (ON CONFLICT DO NOTHING + status transitions) and
rows are never deleted — only their status changes.

Rows come back as plain dicts (`row["col"]`). A re-entrant lock guards the shared
connection so the scheduler, API, and bot threads can't interleave on one socket.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg
from psycopg import Cursor
from psycopg.rows import dict_row

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.memory")

# Seconds to wait for the initial TCP connect before giving up with a readable error.
CONNECT_TIMEOUT = 10


def _redact(dsn: str) -> str:
    """`postgresql://user:pw@host:port/db` -> `postgresql://user@host:port/db` (for error text)."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1@", dsn)


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           SERIAL PRIMARY KEY,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,              -- source's own id or a url hash
    url          TEXT,
    title        TEXT,
    company      TEXT,
    category     TEXT,                        -- transcription | cloud_devops | internship | other
    score        INTEGER,
    summary      TEXT,
    raw_json     TEXT,
    apply_kind   TEXT,                         -- email | portal | notify_only
    apply_target TEXT,                         -- apply email address or apply/careers url
    cover_text   TEXT,                         -- drafted cover email (never invented facts)
    cv_variant   TEXT,                         -- path to tailored CV (Phase 15) if any
    status       TEXT NOT NULL DEFAULT 'new',  -- new|scored|drafted|notified|approved|skipped|applied
    first_seen   DOUBLE PRECISION NOT NULL,
    updated_at   DOUBLE PRECISION NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS emails (
    id           SERIAL PRIMARY KEY,
    gmail_id     TEXT NOT NULL UNIQUE,
    thread_id    TEXT,
    category     TEXT,                        -- promotion|newsletter|social|job_related|important|personal
    subject      TEXT,
    sender       TEXT,
    action       TEXT,                        -- archived | labelled | drafted | none
    status       TEXT NOT NULL DEFAULT 'processed',
    processed_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id             SERIAL PRIMARY KEY,
    job_id         INTEGER,
    company        TEXT,
    source         TEXT,
    category       TEXT,
    applied_at     DOUBLE PRECISION,
    status         TEXT NOT NULL DEFAULT 'applied',  -- applied|replied|interview|offer|rejected
    cv_variant_used TEXT,
    notes          TEXT,
    updated_at     DOUBLE PRECISION NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS qa_log (
    id          SERIAL PRIMARY KEY,
    kind        TEXT,                          -- cover_letter | form_answer | draft ...
    context     TEXT,
    draft       TEXT,
    edited      TEXT,
    distilled   INTEGER NOT NULL DEFAULT 0,    -- 0 until the nightly learning job processes it
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_facts (
    id          SERIAL PRIMARY KEY,
    category    TEXT NOT NULL,                 -- bio|education|work_history|skills|tools|languages|
                                               -- availability|rates|preferences|writing_style|stories|academics
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    source      TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    updated_at  DOUBLE PRECISION NOT NULL,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS standing_instructions (
    id          SERIAL PRIMARY KEY,
    instruction TEXT NOT NULL UNIQUE,
    active      INTEGER NOT NULL DEFAULT 1,     -- soft-delete via active=0, never DELETE
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS cv_facts (
    id          SERIAL PRIMARY KEY,
    section     TEXT NOT NULL,                 -- summary|experience|skills|education|projects|certs
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    cv_version  TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    updated_at  DOUBLE PRECISION NOT NULL,
    UNIQUE(section, key)
);

CREATE TABLE IF NOT EXISTS cv_fact_history (
    id          SERIAL PRIMARY KEY,
    cv_version  TEXT NOT NULL,
    section     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    snapshot_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT,
    format      TEXT,                          -- online | physical
    location    TEXT,
    date        TEXT,
    tags        TEXT,
    url         TEXT,
    free        INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'new',    -- new | notified | interested | skipped
    first_seen  DOUBLE PRECISION NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS vault_files (
    id          SERIAL PRIMARY KEY,
    unit        TEXT NOT NULL,
    file        TEXT NOT NULL,               -- filename within the unit folder
    file_hash   TEXT NOT NULL,               -- content hash; re-ingest only when it changes
    chunks      INTEGER NOT NULL DEFAULT 0,
    ingested_at DOUBLE PRECISION NOT NULL,
    UNIQUE(unit, file)
);

CREATE TABLE IF NOT EXISTS vault_chunks (
    id          SERIAL PRIMARY KEY,
    unit        TEXT NOT NULL,
    file        TEXT NOT NULL,
    loc         TEXT,                         -- 'p.3' | 'slide 2' | '' (for citation)
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   BYTEA NOT NULL,                -- packed float32 (see core.embeddings)
    dim         INTEGER NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  DOUBLE PRECISION NOT NULL,
    UNIQUE(unit, file, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_vault_chunks_unit ON vault_chunks(unit);

CREATE TABLE IF NOT EXISTS vault_chunk_history (
    id          SERIAL PRIMARY KEY,
    unit        TEXT NOT NULL,
    file        TEXT NOT NULL,
    file_hash   TEXT NOT NULL,
    loc         TEXT,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL,
    embedding   BYTEA NOT NULL,
    dim         INTEGER NOT NULL,
    snapshot_at DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS flashcards (
    id            SERIAL PRIMARY KEY,
    unit          TEXT,
    front         TEXT NOT NULL,
    back          TEXT NOT NULL,
    ease          DOUBLE PRECISION NOT NULL DEFAULT 2.5,     -- SM-2 ease factor (Phase 11)
    interval_days INTEGER NOT NULL DEFAULT 0,
    due_at        DOUBLE PRECISION,
    lapses        INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'candidate',  -- candidate | active | suspended
    source        TEXT,                              -- lecture:<file> | vault | manual
    created_at    DOUBLE PRECISION NOT NULL,
    UNIQUE(unit, front)
);
CREATE INDEX IF NOT EXISTS idx_flashcards_due ON flashcards(status, due_at);

CREATE TABLE IF NOT EXISTS card_reviews (
    id          SERIAL PRIMARY KEY,
    card_id     INTEGER NOT NULL,
    unit        TEXT,
    grade       TEXT NOT NULL,          -- again | hard | good | easy
    reviewed_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY(card_id) REFERENCES flashcards(id)
);

CREATE TABLE IF NOT EXISTS deadlines (
    id          SERIAL PRIMARY KEY,
    unit        TEXT,
    title       TEXT NOT NULL,
    type        TEXT,                   -- CAT | assignment | exam | lab
    due_at      DOUBLE PRECISION NOT NULL,
    weight      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    status      TEXT NOT NULL DEFAULT 'active',  -- active | pending | done | cancelled
    source      TEXT,                   -- email | manual | voice
    created_at  DOUBLE PRECISION NOT NULL,
    UNIQUE(unit, title, due_at)
);
CREATE INDEX IF NOT EXISTS idx_deadlines_due ON deadlines(status, due_at);

-- ===================== Phase 16: flash-flip deal broker =====================
-- Capital-risk model: AgentOS never spends money and never lets a buyer pay against
-- inventory that isn't locked in. Purchase requires a passing purchase_gate_check
-- (buyer committed AND availability re-confirmed AND Calvin approved).

CREATE TABLE IF NOT EXISTS listings (
    id               SERIAL PRIMARY KEY,
    source           TEXT NOT NULL,              -- jiji | pigiame | ... (never Facebook, §16)
    external_id      TEXT NOT NULL,
    url              TEXT,
    title            TEXT,
    category         TEXT,
    make_model       TEXT,                       -- normalized, for comps
    condition        TEXT,                       -- new | used | refurbished
    asking_price     DOUBLE PRECISION,
    currency         TEXT NOT NULL DEFAULT 'KES',
    seller_ref       TEXT,
    listed_at        DOUBLE PRECISION,           -- when the seller posted it (staleness proxy)
    repost_count     INTEGER NOT NULL DEFAULT 0, -- seller-motivation proxies
    price_drop_count INTEGER NOT NULL DEFAULT 0,
    raw_json         TEXT,
    first_seen       DOUBLE PRECISION NOT NULL,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS scores (
    id                     SERIAL PRIMARY KEY,
    listing_id             INTEGER NOT NULL REFERENCES listings(id),
    comp_median            DOUBLE PRECISION,
    price_gap_pct          DOUBLE PRECISION,
    listing_age_days       DOUBLE PRECISION,
    motivation             INTEGER,              -- reposts + price drops
    category_velocity_days DOUBLE PRECISION,     -- historical time-to-sale (flash-sale filter)
    total                  DOUBLE PRECISION,     -- 0-100
    verdict                TEXT,                 -- pursue | reject
    reason                 TEXT,
    scored_at              DOUBLE PRECISION NOT NULL,
    UNIQUE(listing_id)
);

-- current state per listing (history kept in pipeline_transitions — never deleted)
CREATE TABLE IF NOT EXISTS pipeline_state (
    id         SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id),
    state      TEXT NOT NULL,   -- DISCOVERED|SCORING|NEGOTIATING|LISTED|BUYER_FOUND|
                                -- PURCHASE_GATE|PURCHASED|DELIVERED|EXPIRED|REJECTED
    reason     TEXT,
    entered_at DOUBLE PRECISION NOT NULL,
    UNIQUE(listing_id)
);

CREATE TABLE IF NOT EXISTS pipeline_transitions (
    id           SERIAL PRIMARY KEY,
    listing_id   INTEGER NOT NULL REFERENCES listings(id),
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    reason       TEXT,
    at           DOUBLE PRECISION NOT NULL
);

-- Drafts the bot writes for CALVIN to send himself. There is deliberately no send path:
-- automating a negotiating persona against strangers is out of scope (§16 trust rules).
CREATE TABLE IF NOT EXISTS negotiation_threads (
    id           SERIAL PRIMARY KEY,
    listing_id   INTEGER NOT NULL REFERENCES listings(id),
    kind         TEXT NOT NULL,               -- opening | counter
    draft        TEXT NOT NULL,
    sent_by_calvin_at DOUBLE PRECISION,       -- set only when Calvin confirms HE sent it
    agreed_price DOUBLE PRECISION,            -- locked-in seller price
    created_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS resale_listings (
    id             SERIAL PRIMARY KEY,
    listing_id     INTEGER NOT NULL REFERENCES listings(id),
    platform       TEXT NOT NULL,             -- Phase 17 cross-posts one item to many
    copy_title     TEXT,
    copy_body      TEXT,
    resale_price   DOUBLE PRECISION,
    flash_deadline DOUBLE PRECISION,          -- window end; no buyer by then -> tier drop/expire
    tier           INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'active',  -- active | delisted | expired
    posted_at      DOUBLE PRECISION NOT NULL,
    UNIQUE(listing_id, platform)
);

CREATE TABLE IF NOT EXISTS buyers (
    id           SERIAL PRIMARY KEY,
    listing_id   INTEGER NOT NULL REFERENCES listings(id),
    platform     TEXT,
    handle       TEXT,
    committed    INTEGER NOT NULL DEFAULT 0,  -- firm commitment
    paid         INTEGER NOT NULL DEFAULT 0,
    amount       DOUBLE PRECISION,
    committed_at DOUBLE PRECISION,
    created_at   DOUBLE PRECISION NOT NULL
);

-- The gate that makes "list before you own" safe. Append-only audit of every check.
CREATE TABLE IF NOT EXISTS purchase_gate_checks (
    id                     SERIAL PRIMARY KEY,
    listing_id             INTEGER NOT NULL REFERENCES listings(id),
    buyer_id               INTEGER REFERENCES buyers(id),
    buyer_committed        INTEGER NOT NULL DEFAULT 0,
    availability_confirmed INTEGER NOT NULL DEFAULT 0,
    calvin_approved        INTEGER NOT NULL DEFAULT 0,
    seller_price           DOUBLE PRECISION,
    decision               TEXT NOT NULL,      -- approved | blocked
    reason                 TEXT,
    checked_at             DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_state ON pipeline_state(state);

-- Phase 18: realized margin per flip. Because the seller price is locked at NEGOTIATING and
-- the buyer pays before PURCHASE_GATE, margin is effectively known before any money moves —
-- this RECORDS outcomes, it is not tracking an open speculative position. EXPIRED/REJECTED
-- are logged too (zero margin), so reports show the true hit rate, not just the wins.
-- margin_pct convention: margin_abs / buyer_paid_price (margin on revenue), matching the
-- revenue-based price_gap_pct used by Phase 16 scoring.
CREATE TABLE IF NOT EXISTS margin_ledger (
    id               SERIAL PRIMARY KEY,
    flip_id          INTEGER NOT NULL REFERENCES listings(id),
    category         TEXT,
    seller_price     DOUBLE PRECISION,      -- locked at NEGOTIATING
    resale_price     DOUBLE PRECISION,      -- listed at LISTED
    buyer_paid_price DOUBLE PRECISION,      -- what the buyer actually paid
    fees             DOUBLE PRECISION NOT NULL DEFAULT 0,
    margin_abs       DOUBLE PRECISION,
    margin_pct       DOUBLE PRECISION,
    status           TEXT NOT NULL DEFAULT 'open',  -- open | closed | expired | rejected
    opened_at        DOUBLE PRECISION NOT NULL,     -- position opened at LISTED
    flash_deadline   DOUBLE PRECISION,
    closed_at        DOUBLE PRECISION,
    UNIQUE(flip_id)
);
CREATE INDEX IF NOT EXISTS idx_margin_status ON margin_ledger(status);

-- ===================== Phase 20: adaptive behavior layer =====================
-- Passive observation only: signals are logged and counted, never acted on. A pattern
-- becomes a candidate rule after it repeats consistently with NO contradicting instance,
-- and even then only Calvin can turn it into a Standing Instruction.
CREATE TABLE IF NOT EXISTS signal_log (
    id            SERIAL PRIMARY KEY,
    skill         TEXT NOT NULL,
    signal_type   TEXT NOT NULL,          -- e.g. event_skipped | job_skipped | card_rejected
    payload       TEXT,                   -- the recurring dimension, e.g. 'Nairobi'
    running_count INTEGER NOT NULL DEFAULT 0,
    contradicted  INTEGER NOT NULL DEFAULT 0,  -- any counter-example blocks candidacy
    status        TEXT NOT NULL DEFAULT 'watching',  -- watching|proposed|confirmed|declined
    first_seen    DOUBLE PRECISION NOT NULL,
    observed_at   DOUBLE PRECISION NOT NULL,
    UNIQUE(skill, signal_type, payload)
);
CREATE INDEX IF NOT EXISTS idx_signal_status ON signal_log(status);

-- ===================== Phase 19: cross-device session continuity =====================
-- AgentOS runs entirely on the VPS, so "use it from any device" is not a sync problem:
-- state lives here and every channel (telegram | voice | dashboard | cli) is a thin client
-- into it. One row per LOGICAL session, keyed to Calvin — never to a device.
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    active_skill      TEXT,
    context_snapshot  TEXT,              -- JSON: the last N turns
    last_channel      TEXT,              -- telegram | voice | dashboard | cli
    pending_approvals TEXT,              -- JSON snapshot at last write
    updated_at        DOUBLE PRECISION NOT NULL
);

-- ===================== Phase 21: self-audit / infra recon =====================
-- REPORT ONLY. This never restarts a service or patches anything — it flags. Findings are
-- deduped on (target, check_type, detail) so a recurring issue ESCALATES rather than being
-- repeated identically forever.
CREATE TABLE IF NOT EXISTS infra_scan_results (
    id          SERIAL PRIMARY KEY,
    target      TEXT NOT NULL,          -- only ever a target Calvin explicitly enrolled
    check_type  TEXT NOT NULL,          -- open_port | tls_expiry | exposed_file | cve | container
    detail      TEXT,
    severity    TEXT NOT NULL,          -- critical | high | medium | low | info
    status      TEXT NOT NULL DEFAULT 'open',   -- open | resolved
    occurrences INTEGER NOT NULL DEFAULT 1,     -- scans in a row it has persisted
    first_seen  DOUBLE PRECISION NOT NULL,
    last_seen   DOUBLE PRECISION NOT NULL,
    UNIQUE(target, check_type, detail)
);
CREATE INDEX IF NOT EXISTS idx_infra_status ON infra_scan_results(status, severity);

-- Every skill's declared scope, persisted at registration so it's inspectable/auditable.
CREATE TABLE IF NOT EXISTS skill_contracts (
    skill_name       TEXT PRIMARY KEY,
    reads_categories TEXT NOT NULL,       -- comma-separated instruction categories
    hard_invariants  TEXT NOT NULL,       -- comma-separated; always includes the §0 five
    registered_at    DOUBLE PRECISION NOT NULL
);
"""


class Memory:
    """Wrapper over a PostgreSQL connection with idempotent helpers.

    `schema` lets a caller (notably the test suite) isolate all tables inside a dedicated
    Postgres schema instead of `public`, giving each test a clean namespace on one server.
    """

    def __init__(self, dsn: str | None = None, schema: str | None = None) -> None:
        self.dsn = dsn or get_settings().database_url
        self.schema = schema
        # psycopg3 connections are thread-safe (each operation is serialized internally);
        # this RLock additionally keeps a multi-statement tx() atomic across threads.
        self._lock = threading.RLock()
        # A DSN pointing at a host that silently drops packets (wrong port behind a firewall)
        # otherwise blocks here forever instead of telling anyone.
        kw = {} if "connect_timeout" in self.dsn else {"connect_timeout": CONNECT_TIMEOUT}
        try:
            self.conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True, **kw)
        except psycopg.OperationalError as exc:
            raise psycopg.OperationalError(
                f"could not connect to {_redact(self.dsn)}: {exc}") from exc
        if schema:
            self.conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
            self.conn.execute(f'SET search_path TO "{schema}"')
        self._init_schema()

    @contextmanager
    def tx(self):
        """Atomic transaction guarded by the connection lock (replaces sqlite's `with conn:`)."""
        with self._lock:
            with self.conn.transaction():
                yield self.conn

    def _init_schema(self) -> None:
        with self.tx():
            self.conn.execute(SCHEMA)      # psycopg runs multi-statement SQL when no params
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (additive only, never drops)."""
        wanted = {
            "jobs": {
                "apply_kind": "TEXT", "apply_target": "TEXT",
                "cover_text": "TEXT", "cv_variant": "TEXT",
            },
            "cv_facts": {"active": "INTEGER NOT NULL DEFAULT 1"},
            "vault_chunks": {"active": "INTEGER NOT NULL DEFAULT 1"},
            # Phase 17: per-platform traction, fed back into category_velocity
            "resale_listings": {"views": "INTEGER NOT NULL DEFAULT 0",
                                "inquiries": "INTEGER NOT NULL DEFAULT 0"},
            # Phase 20: rules are scoped to a category; skills only read their own
            "standing_instructions": {"category": "TEXT NOT NULL DEFAULT 'general'",
                                      "source": "TEXT NOT NULL DEFAULT 'calvin'"},
        }
        with self.tx():
            for table, cols in wanted.items():
                for col, coltype in cols.items():
                    self.conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}")

    # -------------------------------------------------------------- generic kv
    def kv_set(self, key: str, value: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO kv(key, value, updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM kv WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else default

    # -------------------------------------------------------------- jobs
    def upsert_job(
        self,
        source: str,
        external_id: str,
        *,
        url: str | None = None,
        title: str | None = None,
        company: str | None = None,
        raw_json: str | None = None,
    ) -> bool:
        """Insert a newly-seen job. Returns True if this is the first sighting, else False.

        Idempotent: re-seeing the same (source, external_id) does not duplicate or clobber.
        """
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO jobs"
            "(source, external_id, url, title, company, raw_json, status, first_seen, updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s, 'new', %s, %s) ON CONFLICT DO NOTHING",
            (source, external_id, url, title, company, raw_json, now, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_job_status(self, job_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE jobs SET status=%s, updated_at=%s WHERE id=%s",
                (status, time.time(), job_id),
            )

    def score_job(
        self, job_id: int, score: int, category: str | None = None, summary: str | None = None
    ) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE jobs SET score=%s, category=COALESCE(%s,category), "
                "summary=COALESCE(%s,summary), status='scored', updated_at=%s WHERE id=%s",
                (score, category, summary, time.time(), job_id),
            )

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()

    def get_job_by_ref(self, source: str, external_id: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE source=%s AND external_id=%s", (source, external_id)
        ).fetchone()

    def jobs_by_status(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE status=%s ORDER BY score DESC NULLS LAST, updated_at DESC LIMIT %s",
            (status, limit),
        ).fetchall()

    def save_cover(
        self, job_id: int, *, apply_kind: str, apply_target: str | None, cover_text: str,
        cv_variant: str | None = None,
    ) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE jobs SET apply_kind=%s, apply_target=%s, cover_text=%s, cv_variant=%s, "
                "status='drafted', updated_at=%s WHERE id=%s",
                (apply_kind, apply_target, cover_text, cv_variant, time.time(), job_id),
            )

    def applied_company_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT company FROM applications WHERE company IS NOT NULL"
        ).fetchall()
        return [r["company"] for r in rows]

    def record_application(
        self, *, job_id: int | None, company: str | None, source: str | None,
        category: str | None, cv_variant: str | None = None, notes: str | None = None,
    ) -> int:
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO applications(job_id, company, source, category, applied_at, status, "
            "cv_variant_used, notes, updated_at) VALUES(%s,%s,%s,%s,%s, 'applied', %s, %s, %s) "
            "RETURNING id",
            (job_id, company, source, category, now, cv_variant, notes, now),
        )
        if job_id is not None:
            self.conn.execute(
                "UPDATE jobs SET status='applied', updated_at=%s WHERE id=%s", (now, job_id)
            )
        return int(cur.fetchone()["id"])

    def set_application_status(self, app_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE applications SET status=%s, updated_at=%s WHERE id=%s",
                (status, time.time(), app_id),
            )

    def application_stats(self, since: float) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT category, status FROM applications WHERE applied_at >= %s", (since,)
        ).fetchall()
        by_category: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for r in rows:
            by_category[r["category"] or "other"] = by_category.get(r["category"] or "other", 0) + 1
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        return {"total": len(rows), "by_category": by_category, "by_status": by_status}

    # -------------------------------------------------------------- emails
    def record_email(
        self,
        gmail_id: str,
        *,
        thread_id: str | None = None,
        category: str | None = None,
        subject: str | None = None,
        sender: str | None = None,
        action: str = "none",
    ) -> bool:
        """Log a processed email idempotently. Returns True if newly recorded."""
        cur = self.conn.execute(
            "INSERT INTO emails"
            "(gmail_id, thread_id, category, subject, sender, action, status, processed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s, 'processed', %s) ON CONFLICT DO NOTHING",
            (gmail_id, thread_id, category, subject, sender, action, time.time()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def email_seen(self, gmail_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM emails WHERE gmail_id=%s", (gmail_id,)).fetchone()
            is not None
        )

    # -------------------------------------------------------------- persona
    def upsert_fact(
        self,
        category: str,
        key: str,
        value: str,
        *,
        confidence: float = 0.5,
        source: str | None = None,
        verified: bool = False,
    ) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO persona_facts(category, key, value, confidence, source, verified, updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(category, key) DO UPDATE SET "
                "value=excluded.value, confidence=excluded.confidence, "
                "source=excluded.source, verified=excluded.verified, updated_at=excluded.updated_at",
                (category, key, value, confidence, source, int(verified), time.time()),
            )

    def facts_by_category(self, category: str) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM persona_facts WHERE category=%s ORDER BY key", (category,)
        ).fetchall()

    # -------------------------------------------------------------- cv facts (Phase 15)
    def get_cv_facts(self) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM cv_facts WHERE active=1 ORDER BY section, key"
        ).fetchall()

    def replace_cv_facts(self, facts: list[dict[str, Any]], cv_version: str) -> dict[str, Any]:
        """Replace the cv_facts set with a new version. Returns a diff vs the previous set.

        `facts` items: {section, key, value}. Never deletes history destructively beyond the
        working set — the diff is surfaced to Calvin for confirmation.
        """
        old = {(r["section"], r["key"]): r["value"] for r in self.get_cv_facts()}
        new = {(f["section"], f["key"]): f.get("value", "") for f in facts}
        added = [{"section": s, "key": k, "value": v} for (s, k), v in new.items() if (s, k) not in old]
        removed = [{"section": s, "key": k, "value": v} for (s, k), v in old.items() if (s, k) not in new]
        changed = [{"section": s, "key": k, "old": old[(s, k)], "new": v}
                   for (s, k), v in new.items() if (s, k) in old and old[(s, k)] != v]
        now = time.time()
        with self.tx():
            self.conn.execute("UPDATE cv_facts SET active=0 WHERE active=1")
            for f in facts:
                section = f.get("section", "misc")
                key = f.get("key", "")
                value = f.get("value", "")
                self.conn.execute(
                    "INSERT INTO cv_fact_history"
                    "(cv_version, section, key, value, snapshot_at) VALUES(%s,%s,%s,%s,%s)",
                    (cv_version, section, key, value, now),
                )
                self.conn.execute(
                    "INSERT INTO cv_facts(section, key, value, cv_version, active, updated_at) "
                    "VALUES(%s,%s,%s,%s,1,%s) "
                    "ON CONFLICT(section, key) DO UPDATE SET value=excluded.value, "
                    "cv_version=excluded.cv_version, active=1, updated_at=excluded.updated_at",
                    (section, key, value, cv_version, now),
                )
        self.kv_set("cv.version", cv_version)
        return {"added": added, "removed": removed, "changed": changed}

    def set_job_cv_variant(self, job_id: int, path: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE jobs SET cv_variant=%s, updated_at=%s WHERE id=%s",
                              (path, time.time(), job_id))

    # -------------------------------------------------------------- standing instructions
    def add_instruction(self, instruction: str, category: str = "general",
                        source: str = "calvin") -> None:
        """Store a standing rule under a category — skills only read their declared ones."""
        with self.tx():
            self.conn.execute(
                "INSERT INTO standing_instructions(instruction, category, source, active, created_at) "
                "VALUES(%s,%s,%s,1,%s) ON CONFLICT(instruction) DO UPDATE SET active=1, "
                "category=excluded.category, source=excluded.source",
                (instruction.strip(), category, source, time.time()),
            )

    def deactivate_instruction(self, instruction: str) -> None:
        # soft delete — never DELETE (Principle: never delete data)
        with self.tx():
            self.conn.execute(
                "UPDATE standing_instructions SET active=0 WHERE instruction=%s",
                (instruction.strip(),),
            )

    def list_instructions(self, active_only: bool = True,
                          categories: list[str] | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM standing_instructions"
        clauses, params = [], []
        if active_only:
            clauses.append("active=1")
        if categories is not None:
            if not categories:                      # declares nothing -> reads nothing
                return []
            clauses.append("category = ANY(%s)")
            params.append(list(categories))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        return self.conn.execute(sql, params).fetchall()

    # -------------------------------------------------------------- qa log (learning loop)
    def log_edit(self, kind: str, context: str, draft: str, edited: str) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO qa_log(kind, context, draft, edited, distilled, created_at) "
                "VALUES(%s,%s,%s,%s,0,%s)",
                (kind, context, draft, edited, time.time()),
            )

    # -------------------------------------------------------------- events
    def upsert_event(
        self,
        source: str,
        external_id: str,
        *,
        title: str | None = None,
        fmt: str | None = None,
        location: str | None = None,
        date: str | None = None,
        tags: str | None = None,
        url: str | None = None,
        free: bool = True,
    ) -> bool:
        cur = self.conn.execute(
            "INSERT INTO events"
            "(source, external_id, title, format, location, date, tags, url, free, status, first_seen) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s, 'new', %s) ON CONFLICT DO NOTHING",
            (source, external_id, title, fmt, location, date, tags, url, int(free), time.time()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -------------------------------------------------------------- deadlines
    def add_deadline(self, title: str, due_at: float, *, unit: str | None = None,
                     dtype: str | None = None, weight: float = 1.0, status: str = "active",
                     source: str = "manual") -> int:
        """Insert a deadline (idempotent on unit+title+due). Returns row id (existing or new)."""
        with self.tx():
            self.conn.execute(
                "INSERT INTO deadlines(unit, title, type, due_at, weight, status, source, created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (unit, title, dtype, due_at, weight, status, source, time.time()))
        row = self.conn.execute(
            "SELECT id FROM deadlines WHERE unit IS NOT DISTINCT FROM %s AND title=%s AND due_at=%s",
            (unit, title, due_at)).fetchone()
        return int(row["id"]) if row else 0

    def deadlines_within(self, days: int, now: float | None = None,
                         status: str = "active") -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        return self.conn.execute(
            "SELECT * FROM deadlines WHERE status=%s AND due_at>=%s AND due_at<=%s ORDER BY due_at",
            (status, now, now + days * 86400)).fetchall()

    def deadlines_for_unit(self, unit: str, dtype: str | None = None,
                           status: str = "active") -> list[dict[str, Any]]:
        sql = "SELECT * FROM deadlines WHERE unit=%s AND status=%s"
        params: list[Any] = [unit, status]
        if dtype:
            sql += " AND type=%s"
            params.append(dtype)
        sql += " ORDER BY due_at"
        return self.conn.execute(sql, params).fetchall()

    def pending_deadlines(self) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM deadlines WHERE status='pending' ORDER BY due_at").fetchall()

    def set_deadline_status(self, deadline_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE deadlines SET status=%s WHERE id=%s", (status, deadline_id))

    # -------------------------------------------------------------- study vault
    def vault_file_hash(self, unit: str, file: str) -> str | None:
        row = self.conn.execute(
            "SELECT file_hash FROM vault_files WHERE unit=%s AND file=%s", (unit, file)
        ).fetchone()
        return row["file_hash"] if row else None

    def replace_vault_file(self, unit: str, file: str, file_hash: str,
                           chunks: list[dict[str, Any]]) -> int:
        """Idempotently (re)ingest a file while preserving every indexed version.

        `chunks` items: {loc, chunk_index, text, embedding(bytes), dim}. Returns chunk count.
        """
        now = time.time()
        with self.tx():
            self.conn.execute(
                "UPDATE vault_chunks SET active=0 WHERE unit=%s AND file=%s AND active=1",
                (unit, file),
            )
            for c in chunks:
                self.conn.execute(
                    "INSERT INTO vault_chunk_history"
                    "(unit, file, file_hash, loc, chunk_index, text, embedding, dim, snapshot_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (unit, file, file_hash, c["loc"], c["chunk_index"], c["text"],
                     c["embedding"], c["dim"], now),
                )
                self.conn.execute(
                    "INSERT INTO vault_chunks"
                    "(unit, file, loc, chunk_index, text, embedding, dim, active, created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,1,%s) "
                    "ON CONFLICT(unit, file, chunk_index) DO UPDATE SET loc=excluded.loc, "
                    "text=excluded.text, embedding=excluded.embedding, dim=excluded.dim, "
                    "active=1, created_at=excluded.created_at",
                    (unit, file, c["loc"], c["chunk_index"], c["text"],
                     c["embedding"], c["dim"], now),
                )
            self.conn.execute(
                "INSERT INTO vault_files(unit, file, file_hash, chunks, ingested_at) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT(unit, file) DO UPDATE SET file_hash=excluded.file_hash, "
                "chunks=excluded.chunks, ingested_at=excluded.ingested_at",
                (unit, file, file_hash, len(chunks), now),
            )
        return len(chunks)

    def vault_chunks(self, unit: str | None = None) -> list[dict[str, Any]]:
        if unit:
            return self.conn.execute(
                "SELECT * FROM vault_chunks WHERE unit=%s AND active=1", (unit,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM vault_chunks WHERE active=1").fetchall()

    def vault_status(self) -> dict[str, Any]:
        units = self.conn.execute(
            "SELECT unit, COUNT(DISTINCT file) files, COUNT(*) chunks, MAX(ingested_at) last "
            "FROM vault_files GROUP BY unit"
        ).fetchall()
        total_chunks = self.conn.execute(
            "SELECT COUNT(*) c FROM vault_chunks WHERE active=1"
        ).fetchone()["c"]
        return {
            "units": [dict(u) for u in units],
            "total_chunks": total_chunks,
            "last_ingested": self.conn.execute(
                "SELECT MAX(ingested_at) m FROM vault_files").fetchone()["m"],
        }

    # -------------------------------------------------------------- flashcards
    def add_flashcard(self, front: str, back: str, *, unit: str | None = None,
                      source: str | None = None, status: str = "candidate") -> bool:
        """Insert a flashcard idempotently (UNIQUE unit+front). Returns True if newly added."""
        cur = self.conn.execute(
            "INSERT INTO flashcards(unit, front, back, status, source, created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (unit, front, back, status, source, time.time()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_flashcard(self, card_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM flashcards WHERE id=%s", (card_id,)).fetchone()

    def candidate_cards(self, unit: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM flashcards WHERE status='candidate'"
        params: list[Any] = []
        if unit:
            sql += " AND unit=%s"
            params.append(unit)
        sql += " ORDER BY created_at LIMIT %s"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def due_cards(self, unit: str | None = None, now: float | None = None,
                  limit: int = 30) -> list[dict[str, Any]]:
        now = now if now is not None else time.time()
        sql = "SELECT * FROM flashcards WHERE status='active' AND (due_at IS NULL OR due_at<=%s)"
        params: list[Any] = [now]
        if unit:
            sql += " AND unit=%s"
            params.append(unit)
        sql += " ORDER BY due_at LIMIT %s"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def approve_card(self, card_id: int, now: float | None = None) -> None:
        """Activate a candidate card, due immediately (fresh SM-2 state)."""
        now = now if now is not None else time.time()
        with self.tx():
            self.conn.execute(
                "UPDATE flashcards SET status='active', ease=2.5, interval_days=0, due_at=%s "
                "WHERE id=%s", (now, card_id))

    def reject_card(self, card_id: int) -> None:
        # soft-delete: suspend, never DELETE (§0)
        with self.tx():
            self.conn.execute("UPDATE flashcards SET status='suspended' WHERE id=%s", (card_id,))

    def edit_card(self, card_id: int, front: str | None = None, back: str | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE flashcards SET front=COALESCE(%s,front), back=COALESCE(%s,back) WHERE id=%s",
                (front, back, card_id))

    def update_card_schedule(self, card_id: int, *, ease: float, interval_days: int,
                             lapses: int, due_at: float) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE flashcards SET ease=%s, interval_days=%s, lapses=%s, due_at=%s WHERE id=%s",
                (ease, interval_days, lapses, due_at, card_id))

    def log_review(self, card_id: int, unit: str | None, grade: str, now: float | None = None) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO card_reviews(card_id, unit, grade, reviewed_at) VALUES(%s,%s,%s,%s)",
                (card_id, unit, grade, now if now is not None else time.time()))

    def retention_stats(self, since: float) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT unit, grade, COUNT(*) c FROM card_reviews WHERE reviewed_at>=%s GROUP BY unit, grade",
            (since,)).fetchall()
        by_unit: dict[str, dict[str, int]] = {}
        for r in rows:
            unit = r["unit"] or "UNSORTED"
            by_unit.setdefault(unit, {}).update({r["grade"]: r["c"]})
        report = {}
        for unit, grades in by_unit.items():
            total = sum(grades.values())
            passed = grades.get("good", 0) + grades.get("easy", 0)
            report[unit] = {"reviews": total, "retention": round(passed / total, 2) if total else 0.0,
                            "grades": grades}
        return report

    def weakest_cards(self, unit: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        sql = "SELECT * FROM flashcards WHERE status='active'"
        params: list[Any] = []
        if unit:
            sql += " AND unit=%s"
            params.append(unit)
        sql += " ORDER BY lapses DESC, ease ASC LIMIT %s"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def surge_unit(self, unit: str, now: float | None = None, ease_below: float = 2.3) -> int:
        """Make a unit's weak active cards due now (exam-approaching cram). Returns count."""
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "UPDATE flashcards SET due_at=%s WHERE status='active' AND unit=%s AND "
            "(ease<%s OR lapses>0)", (now, unit, ease_below))
        self.conn.commit()
        return cur.rowcount

    def count_flashcards(self, unit: str | None = None, status: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) c FROM flashcards", []
        clauses = []
        if unit:
            clauses.append("unit=%s")
            params.append(unit)
        if status:
            clauses.append("status=%s")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql, params).fetchone()["c"]

    # -------------------------------------------------------------- events (Phase 14)
    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM events WHERE id=%s", (event_id,)).fetchone()

    def events_by_status(self, status: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM events WHERE status=%s ORDER BY date LIMIT %s", (status, limit)).fetchall()

    def set_event_status(self, event_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE events SET status=%s WHERE id=%s", (status, event_id))

    def event_by_ref(self, source: str, external_id: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM events WHERE source=%s AND external_id=%s", (source, external_id)).fetchone()

    # -------------------------------------------------------------- flip pipeline (Phase 16)
    def upsert_listing(self, source: str, external_id: str, **f: Any) -> bool:
        """Insert a newly-seen marketplace listing. True if first sighting (idempotent)."""
        now = time.time()
        cur = self.conn.execute(
            "INSERT INTO listings(source, external_id, url, title, category, make_model, "
            "condition, asking_price, currency, seller_ref, listed_at, repost_count, "
            "price_drop_count, raw_json, first_seen) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (source, external_id, f.get("url"), f.get("title"), f.get("category"),
             f.get("make_model"), f.get("condition"), f.get("asking_price"),
             f.get("currency", "KES"), f.get("seller_ref"), f.get("listed_at"),
             int(f.get("repost_count", 0)), int(f.get("price_drop_count", 0)),
             f.get("raw_json"), now))
        return cur.rowcount > 0

    def get_listing(self, listing_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM listings WHERE id=%s", (listing_id,)).fetchone()

    def listing_by_ref(self, source: str, external_id: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM listings WHERE source=%s AND external_id=%s",
            (source, external_id)).fetchone()

    def comp_median(self, make_model: str, condition: str | None = None,
                    exclude_id: int | None = None) -> float | None:
        """Median asking price of comparable listings (same make/model, optionally condition)."""
        sql = ("SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY asking_price) AS m "
               "FROM listings WHERE make_model=%s AND asking_price IS NOT NULL")
        params: list[Any] = [make_model]
        if condition:
            sql += " AND condition=%s"
            params.append(condition)
        if exclude_id:
            sql += " AND id<>%s"
            params.append(exclude_id)
        row = self.conn.execute(sql, params).fetchone()
        return float(row["m"]) if row and row["m"] is not None else None

    def save_score(self, listing_id: int, **f: Any) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO scores(listing_id, comp_median, price_gap_pct, listing_age_days, "
                "motivation, category_velocity_days, total, verdict, reason, scored_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(listing_id) DO UPDATE SET comp_median=excluded.comp_median, "
                "price_gap_pct=excluded.price_gap_pct, listing_age_days=excluded.listing_age_days, "
                "motivation=excluded.motivation, category_velocity_days=excluded.category_velocity_days, "
                "total=excluded.total, verdict=excluded.verdict, reason=excluded.reason, "
                "scored_at=excluded.scored_at",
                (listing_id, f.get("comp_median"), f.get("price_gap_pct"), f.get("listing_age_days"),
                 f.get("motivation"), f.get("category_velocity_days"), f.get("total"),
                 f.get("verdict"), f.get("reason"), time.time()))

    def get_score(self, listing_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM scores WHERE listing_id=%s", (listing_id,)).fetchone()

    # ---- state machine
    def get_state(self, listing_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT state FROM pipeline_state WHERE listing_id=%s", (listing_id,)).fetchone()
        return row["state"] if row else None

    def set_state(self, listing_id: int, state: str, reason: str | None = None) -> None:
        """Set current state and append to the immutable transition log."""
        now = time.time()
        prev = self.get_state(listing_id)
        with self.tx():
            self.conn.execute(
                "INSERT INTO pipeline_state(listing_id, state, reason, entered_at) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(listing_id) DO UPDATE SET state=excluded.state, "
                "reason=excluded.reason, entered_at=excluded.entered_at",
                (listing_id, state, reason, now))
            self.conn.execute(
                "INSERT INTO pipeline_transitions(listing_id, from_state, to_state, reason, at) "
                "VALUES(%s,%s,%s,%s,%s)", (listing_id, prev, state, reason, now))

    def listings_in_state(self, state: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT l.*, p.state, p.entered_at FROM listings l JOIN pipeline_state p "
            "ON p.listing_id=l.id WHERE p.state=%s ORDER BY p.entered_at LIMIT %s",
            (state, limit)).fetchall()

    def transitions(self, listing_id: int) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM pipeline_transitions WHERE listing_id=%s ORDER BY at", (listing_id,)).fetchall()

    # ---- negotiation (draft-only; Calvin sends)
    def add_negotiation_draft(self, listing_id: int, kind: str, draft: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO negotiation_threads(listing_id, kind, draft, created_at) "
            "VALUES(%s,%s,%s,%s) RETURNING id", (listing_id, kind, draft, time.time()))
        return int(cur.fetchone()["id"])

    def mark_negotiation_sent(self, thread_id: int, agreed_price: float | None = None) -> None:
        """Record that CALVIN sent the message himself (and any price he locked in)."""
        with self.tx():
            self.conn.execute(
                "UPDATE negotiation_threads SET sent_by_calvin_at=%s, agreed_price=COALESCE(%s, agreed_price) "
                "WHERE id=%s", (time.time(), agreed_price, thread_id))

    def agreed_price(self, listing_id: int) -> float | None:
        row = self.conn.execute(
            "SELECT agreed_price FROM negotiation_threads WHERE listing_id=%s AND agreed_price IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1", (listing_id,)).fetchone()
        return float(row["agreed_price"]) if row else None

    # ---- resale listings + flash window
    def add_resale_listing(self, listing_id: int, platform: str, *, copy_title: str,
                           copy_body: str, resale_price: float, flash_deadline: float,
                           posted_at: float | None = None) -> None:
        posted_at = posted_at if posted_at is not None else time.time()
        with self.tx():
            self.conn.execute(
                "INSERT INTO resale_listings(listing_id, platform, copy_title, copy_body, "
                "resale_price, flash_deadline, posted_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(listing_id, platform) DO UPDATE SET copy_title=excluded.copy_title, "
                "copy_body=excluded.copy_body, resale_price=excluded.resale_price, "
                "flash_deadline=excluded.flash_deadline, status='active'",
                (listing_id, platform, copy_title, copy_body, resale_price, flash_deadline, posted_at))

    def resale_listings(self, listing_id: int, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM resale_listings WHERE listing_id=%s"
        if active_only:
            sql += " AND status='active'"
        return self.conn.execute(sql, (listing_id,)).fetchall()

    def expiring_resale(self, now: float) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM resale_listings WHERE status='active' AND flash_deadline<=%s", (now,)).fetchall()

    def set_resale_status(self, resale_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE resale_listings SET status=%s WHERE id=%s", (status, resale_id))

    def drop_resale_tier(self, resale_id: int, new_price: float, new_deadline: float) -> None:
        with self.tx():
            self.conn.execute(
                "UPDATE resale_listings SET resale_price=%s, flash_deadline=%s, tier=tier+1 WHERE id=%s",
                (new_price, new_deadline, resale_id))

    # ---- multi-platform distribution (Phase 17)
    def delist_others(self, listing_id: int, keep_platform: str) -> int:
        """First-committed-buyer wins: pull the item from every OTHER platform at once.

        This is what stops a second buyer paying for something that no longer exists.
        """
        with self.tx():
            cur = self.conn.execute(
                "UPDATE resale_listings SET status='delisted' WHERE listing_id=%s "
                "AND platform<>%s AND status='active'", (listing_id, keep_platform))
        return cur.rowcount

    def active_platforms(self, listing_id: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT platform FROM resale_listings WHERE listing_id=%s AND status='active' "
            "ORDER BY platform", (listing_id,)).fetchall()
        return [r["platform"] for r in rows]

    def record_platform_stats(self, listing_id: int, platform: str, *, views: int = 0,
                              inquiries: int = 0) -> None:
        """Store platform-reported traction (only some platforms expose these)."""
        with self.tx():
            self.conn.execute(
                "UPDATE resale_listings SET views=%s, inquiries=%s WHERE listing_id=%s AND platform=%s",
                (int(views), int(inquiries), listing_id, platform))

    def listings_with_expiring_windows(self, now: float) -> list[int]:
        """Distinct listing ids whose flash window has closed (tier drops apply uniformly)."""
        rows = self.conn.execute(
            "SELECT DISTINCT listing_id FROM resale_listings WHERE status='active' "
            "AND flash_deadline<=%s", (now,)).fetchall()
        return [r["listing_id"] for r in rows]

    def observed_velocity(self, category: str, min_samples: int = 3) -> float | None:
        """Median real days-to-committed-buyer for a category, or None until enough samples.

        This is the Phase 17 analytics feedback: once we have real outcomes they beat the
        configured guess in Phase 16's scoring.
        """
        row = self.conn.execute(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY d) AS m, COUNT(*) AS n FROM ("
            "  SELECT (MIN(b.committed_at) - MIN(r.posted_at))/86400.0 AS d"
            "  FROM buyers b"
            "  JOIN listings l ON l.id = b.listing_id"
            "  JOIN resale_listings r ON r.listing_id = b.listing_id"
            "  WHERE l.category=%s AND (b.committed=1 OR b.paid=1) AND b.committed_at IS NOT NULL"
            "  GROUP BY b.listing_id"
            ") t", (category,)).fetchone()
        if not row or row["n"] is None or row["n"] < min_samples or row["m"] is None:
            return None
        return float(row["m"])

    # ---- buyers
    def add_buyer(self, listing_id: int, *, platform: str, handle: str, committed: bool = False,
                  paid: bool = False, amount: float | None = None,
                  now: float | None = None) -> int:
        now = now if now is not None else time.time()
        cur = self.conn.execute(
            "INSERT INTO buyers(listing_id, platform, handle, committed, paid, amount, "
            "committed_at, created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (listing_id, platform, handle, int(committed), int(paid), amount,
             now if (committed or paid) else None, now))
        return int(cur.fetchone()["id"])

    def get_buyer(self, buyer_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM buyers WHERE id=%s", (buyer_id,)).fetchone()

    def committed_buyer(self, listing_id: int) -> dict[str, Any] | None:
        """The first buyer who paid or firmly committed (first-committed-buyer wins, Phase 17)."""
        return self.conn.execute(
            "SELECT * FROM buyers WHERE listing_id=%s AND (committed=1 OR paid=1) "
            "ORDER BY COALESCE(committed_at, created_at) LIMIT 1", (listing_id,)).fetchone()

    # ---- purchase gate (append-only audit)
    def log_gate_check(self, listing_id: int, *, buyer_id: int | None, buyer_committed: bool,
                       availability_confirmed: bool, calvin_approved: bool,
                       seller_price: float | None, decision: str, reason: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO purchase_gate_checks(listing_id, buyer_id, buyer_committed, "
            "availability_confirmed, calvin_approved, seller_price, decision, reason, checked_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (listing_id, buyer_id, int(buyer_committed), int(availability_confirmed),
             int(calvin_approved), seller_price, decision, reason, time.time()))
        return int(cur.fetchone()["id"])

    def gate_checks(self, listing_id: int) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM purchase_gate_checks WHERE listing_id=%s ORDER BY checked_at",
            (listing_id,)).fetchall()

    def has_passing_gate(self, listing_id: int) -> bool:
        """True only if an APPROVED gate check exists — the precondition for any purchase."""
        row = self.conn.execute(
            "SELECT 1 FROM purchase_gate_checks WHERE listing_id=%s AND decision='approved' "
            "AND buyer_committed=1 AND availability_confirmed=1 AND calvin_approved=1 LIMIT 1",
            (listing_id,)).fetchone()
        return row is not None

    def set_email_action(self, gmail_id: str, action: str) -> None:
        """Update the audit status for an email without removing its history row."""
        with self.tx():
            self.conn.execute("UPDATE emails SET action=%s WHERE gmail_id=%s", (action, gmail_id))

    # -------------------------------------------------------------- margin ledger (Phase 18)
    def open_position(self, flip_id: int, *, category: str | None, seller_price: float | None,
                      resale_price: float | None, flash_deadline: float | None,
                      now: float | None = None) -> None:
        """Open a ledger position when the item is LISTED. Idempotent per flip."""
        now = now if now is not None else time.time()
        with self.tx():
            self.conn.execute(
                "INSERT INTO margin_ledger(flip_id, category, seller_price, resale_price, "
                "flash_deadline, status, opened_at) VALUES(%s,%s,%s,%s,%s,'open',%s) "
                "ON CONFLICT(flip_id) DO UPDATE SET resale_price=excluded.resale_price, "
                "flash_deadline=excluded.flash_deadline",
                (flip_id, category, seller_price, resale_price, flash_deadline, now))

    def close_position(self, flip_id: int, *, buyer_paid_price: float, fees: float = 0.0,
                       now: float | None = None) -> dict[str, Any] | None:
        """Resolve a position at PURCHASE_GATE. Computes margin from real numbers."""
        now = now if now is not None else time.time()
        row = self.conn.execute(
            "SELECT * FROM margin_ledger WHERE flip_id=%s", (flip_id,)).fetchone()
        if not row:
            return None
        seller = float(row["seller_price"] or 0)
        margin_abs = round(float(buyer_paid_price) - seller - float(fees), 2)
        margin_pct = round(margin_abs / float(buyer_paid_price), 4) if buyer_paid_price else 0.0
        with self.tx():
            self.conn.execute(
                "UPDATE margin_ledger SET buyer_paid_price=%s, fees=%s, margin_abs=%s, "
                "margin_pct=%s, status='closed', closed_at=%s WHERE flip_id=%s",
                (float(buyer_paid_price), float(fees), margin_abs, margin_pct, now, flip_id))
        return self.get_position(flip_id)

    def mark_position(self, flip_id: int, status: str, now: float | None = None) -> None:
        """Log a non-winning outcome (expired/rejected) — zero margin, but it counts as an attempt."""
        now = now if now is not None else time.time()
        with self.tx():
            self.conn.execute(
                "UPDATE margin_ledger SET status=%s, margin_abs=COALESCE(margin_abs,0), "
                "margin_pct=COALESCE(margin_pct,0), closed_at=%s WHERE flip_id=%s AND status='open'",
                (status, now, flip_id))

    def get_position(self, flip_id: int) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM margin_ledger WHERE flip_id=%s", (flip_id,)).fetchone()

    def ledger_stats(self, since: float) -> dict[str, Any]:
        """Attempts vs. outcomes since `since` — the true hit rate, not just the wins."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n, COALESCE(SUM(margin_abs),0) total, AVG(margin_pct) avg_pct, "
            "AVG((closed_at-opened_at)/86400.0) avg_days FROM margin_ledger "
            "WHERE opened_at>=%s GROUP BY status", (since,)).fetchall()
        by_status = {r["status"]: {"n": r["n"], "total": float(r["total"] or 0),
                                   "avg_pct": float(r["avg_pct"]) if r["avg_pct"] is not None else None,
                                   "avg_days": float(r["avg_days"]) if r["avg_days"] is not None else None}
                     for r in rows}
        attempted = sum(v["n"] for v in by_status.values())
        closed = by_status.get("closed", {}).get("n", 0)
        cats = self.conn.execute(
            "SELECT category, AVG(margin_pct) m, COUNT(*) n FROM margin_ledger "
            "WHERE status='closed' AND opened_at>=%s AND category IS NOT NULL "
            "GROUP BY category ORDER BY m DESC", (since,)).fetchall()
        return {
            "attempted": attempted,
            "completed": closed,
            "expired": by_status.get("expired", {}).get("n", 0),
            "rejected": by_status.get("rejected", {}).get("n", 0),
            "open": by_status.get("open", {}).get("n", 0),
            "total_margin": by_status.get("closed", {}).get("total", 0.0),
            "avg_margin_pct": by_status.get("closed", {}).get("avg_pct"),
            "avg_days_to_close": by_status.get("closed", {}).get("avg_days"),
            "hit_rate": round(closed / attempted, 2) if attempted else 0.0,
            "by_category": [{"category": c["category"], "avg_margin_pct": float(c["m"]),
                             "n": c["n"]} for c in cats],
        }

    def category_margin(self, category: str, min_samples: int = 2) -> float | None:
        """Real average margin % for a category — None until there's enough history."""
        row = self.conn.execute(
            "SELECT AVG(margin_pct) m, COUNT(*) n FROM margin_ledger "
            "WHERE status='closed' AND category=%s", (category,)).fetchone()
        if not row or not row["n"] or row["n"] < min_samples or row["m"] is None:
            return None
        return float(row["m"])

    # -------------------------------------------------------------- adaptive layer (Phase 20)
    def log_signal(self, skill: str, signal_type: str, payload: str | None = None, *,
                   contradicts: bool = False, now: float | None = None) -> None:
        """Passively record an interaction signal. Never acts — just counts.

        `contradicts=True` records a counter-example, which blocks the pattern from ever
        being proposed (a rule needs consistency, not a majority).
        """
        now = now if now is not None else time.time()
        with self.tx():
            self.conn.execute(
                "INSERT INTO signal_log(skill, signal_type, payload, running_count, "
                "contradicted, first_seen, observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(skill, signal_type, payload) DO UPDATE SET "
                "running_count = signal_log.running_count + %s, "
                "contradicted = signal_log.contradicted + %s, observed_at = excluded.observed_at",
                (skill, signal_type, payload, 0 if contradicts else 1, 1 if contradicts else 0,
                 now, now, 0 if contradicts else 1, 1 if contradicts else 0))

    def signal_candidates(self, threshold: int = 4) -> list[dict[str, Any]]:
        """Patterns consistent enough to propose: repeated >= threshold, never contradicted."""
        return self.conn.execute(
            "SELECT * FROM signal_log WHERE status='watching' AND contradicted=0 "
            "AND running_count>=%s ORDER BY running_count DESC", (threshold,)).fetchall()

    def get_signal(self, signal_id: int) -> dict[str, Any] | None:
        return self.conn.execute("SELECT * FROM signal_log WHERE id=%s", (signal_id,)).fetchone()

    def set_signal_status(self, signal_id: int, status: str) -> None:
        with self.tx():
            self.conn.execute("UPDATE signal_log SET status=%s WHERE id=%s", (status, signal_id))

    def register_contract(self, skill_name: str, reads_categories: list[str],
                          hard_invariants: list[str]) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO skill_contracts(skill_name, reads_categories, hard_invariants, "
                "registered_at) VALUES(%s,%s,%s,%s) ON CONFLICT(skill_name) DO UPDATE SET "
                "reads_categories=excluded.reads_categories, "
                "hard_invariants=excluded.hard_invariants, registered_at=excluded.registered_at",
                (skill_name, ",".join(reads_categories), ",".join(hard_invariants), time.time()))

    def get_contracts(self) -> list[dict[str, Any]]:
        return self.conn.execute(
            "SELECT * FROM skill_contracts ORDER BY skill_name").fetchall()

    def skills_reading(self, category: str) -> list[str]:
        """Which skills declare that they read this instruction category (boundary check)."""
        rows = self.conn.execute("SELECT skill_name, reads_categories FROM skill_contracts").fetchall()
        return [r["skill_name"] for r in rows
                if category in [c for c in r["reads_categories"].split(",") if c]]

    # -------------------------------------------------------------- infra recon (Phase 21)
    def record_finding(self, target: str, check_type: str, detail: str, severity: str,
                       now: float | None = None) -> int:
        """Upsert a finding. Re-seeing it increments occurrences (drives escalation)."""
        now = now if now is not None else time.time()
        with self.tx():
            self.conn.execute(
                "INSERT INTO infra_scan_results(target, check_type, detail, severity, status, "
                "occurrences, first_seen, last_seen) VALUES(%s,%s,%s,%s,'open',1,%s,%s) "
                "ON CONFLICT(target, check_type, detail) DO UPDATE SET "
                "occurrences = infra_scan_results.occurrences + 1, last_seen=excluded.last_seen, "
                "severity=excluded.severity, status='open'",
                (target, check_type, detail, severity, now, now))
        row = self.conn.execute(
            "SELECT occurrences FROM infra_scan_results WHERE target=%s AND check_type=%s "
            "AND detail IS NOT DISTINCT FROM %s", (target, check_type, detail)).fetchone()
        return int(row["occurrences"]) if row else 1

    def resolve_unseen(self, target: str, scan_started: float) -> int:
        """Findings not re-observed in this scan are resolved (kept, never deleted — §0 P4)."""
        with self.tx():
            cur = self.conn.execute(
                "UPDATE infra_scan_results SET status='resolved' WHERE target=%s "
                "AND status='open' AND last_seen < %s", (target, scan_started))
        return cur.rowcount

    def open_findings(self, target: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT * FROM infra_scan_results WHERE status='open'")
        params: list[Any] = []
        if target:
            sql += " AND target=%s"
            params.append(target)
        sql += (" ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
                "WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, occurrences DESC")
        return self.conn.execute(sql, params).fetchall()

    # -------------------------------------------------------------- utilities
    def execute(self, sql: str, params: Iterable[Any] = ()) -> Cursor:
        return self.conn.execute(sql, tuple(params))

    def close(self) -> None:
        self.conn.close()


_default_memory: Memory | None = None


def get_memory() -> Memory:
    """Return a lazily-constructed process-wide Memory instance."""
    global _default_memory
    if _default_memory is None:
        _default_memory = Memory()
    return _default_memory
