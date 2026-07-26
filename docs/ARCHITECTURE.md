# AgentOS — Architecture & How Everything Works

This document explains **what** AgentOS is, **how** every part is built, and **why** it's
built that way. Read [`DEPLOYMENT.md`](DEPLOYMENT.md) for running it in production.

- [1. Overview](#1-overview)
- [2. §0 Non-negotiable principles (and how each is enforced)](#2-0-non-negotiable-principles)
- [3. High-level architecture](#3-high-level-architecture)
- [4. The kernel](#4-the-kernel)
- [5. `core/` — shared infrastructure](#5-core--shared-infrastructure)
- [6. Model routing & concurrency](#6-model-routing--concurrency)
- [7. Data model (PostgreSQL)](#7-data-model-postgresql)
- [8. The skills, phase by phase](#8-the-skills-phase-by-phase)
- [9. Conversational state machines](#9-conversational-state-machines)
- [10. Testing philosophy](#10-testing-philosophy)
- [11. Adding a new capability](#11-adding-a-new-capability)

---

## 1. Overview

AgentOS is a **headless agent** that runs 24/7 on a DigitalOcean droplet, plus thin clients:
a voice client on Calvin's laptop, Telegram / a phone shortcut, and a browser dashboard. It
does five jobs:

1. **Job-hunter / career assistant** — finds jobs, scores them against Calvin's profile,
   drafts applications from his verified facts, tracks everything, and preps him for interviews.
2. **Study companion** — RAG over his course notes, lecture-audio → notes/flashcards, spaced
   repetition, a code tutor, and a semester planner that ties it all into one morning briefing.
3. **Deal broker** — sources underpriced local listings, scores them, drafts negotiations for
   Calvin to send, runs a resale flash window, and books the margin (Phases 16–18).
4. **Self-audit** — a weekly report-only security pass over infrastructure Calvin enrols
   (Phase 21).
5. **Ambient** — an adaptive layer that proposes rules from observed patterns (Phase 20), a
   cross-device session (Phase 19), and a music companion (Phase 22).

Everything is reachable four ways — **voice**, **Telegram**, **dashboard**, **CLI** — and all
four funnel through *one* intent router and *one* set of skills, so behaviour is identical
everywhere. They also share **one server-side session keyed to Calvin, not to a device**, so
a task started in one channel continues in another.

---

## 2. §0 Non-negotiable principles

These are hard rules. Each is enforced in code and covered by tests.

| # | Principle | How it's enforced |
|---|-----------|-------------------|
| 1 | **Free-first LLMs (NIM only)** | All model calls go through `core/llm.py` → NVIDIA NIM. No other provider is imported anywhere. |
| 2 | **Model routing, never hardcoding** | Every call passes a *task class*; `config.yaml → llm.routes` maps class→model. No model id is hardcoded in a skill. |
| 3 | **Approval gates** | Job applications & form submits require Calvin's approval (`approve`, Telegram buttons) — there is no flag that relaxes this. `AUTO_APPLY` used to, and was removed: a config switch that reaches "never ask before sending in his name" is the same gap Phase 30 closes by keeping `high` out of `LEARNABLE_TIERS`. Email replies are **draft-only**; `compose` sends only after an exact typed confirmation. Tests assert `hunt()` never sends, and that no `auto_apply`-shaped field exists on `Settings`. |
| 4 | **Never permanently delete data** | Scheduled cleanup only archives+labels. A user's explicit request may move previewed, confirmed messages to recoverable Gmail Trash and offers undo; permanent Gmail deletion is not exposed. DB rows use status changes / soft-deletes (`suspended`, `cancelled`, `active=0`), never `DELETE`. |
| 5 | **Never fabricate facts about Calvin** | `persona.answer()` returns `NEEDS_INPUT` on any gap; form answers flag unknowns; CV tailoring has an anti-fabrication check (`core/ats.fabrication_terms`). Multiple tests. |
| 6 | **Everything is a Skill** | Skills live in `skills/`, auto-discovered by `kernel/registry.py`. Adding one never touches the kernel. |
| 7 | **Idempotent + resumable** | Every scraped job / processed email / event / ingested file is de-duplicated in Postgres (`ON CONFLICT DO NOTHING` + unique keys). Re-runs process zero duplicates. |
| 8 | **No face cloning** | No image/avatar generation of Calvin's face anywhere. |
| 9 | **No voice cloning** | Voice layer is restricted to pre-built edge-tts neural voices; `skills/voice.py` refuses anything else. A test (`test_no_voice_or_face_cloning_code_path`) scans the entire codebase for banned cloning imports/functions and fails if any appear. |

Principles 8 and 9 **cannot be relaxed by any config flag** — there is no setting that turns
them off. Every skill's contract (§8, Phase 20) re-adds them even if the
skill omits them, so a skill cannot opt out either.

The addendum phases (16–22) added three more, enforced the same way:

| # | Principle | How it's enforced |
|---|-----------|-------------------|
| 10 | **Never spend money** | There is no payment integration anywhere in the codebase. `confirm_purchase` refuses unless a **purchase gate** passes: a committed buyer, re-confirmed availability, and Calvin's approval. Capital is never at risk against unlocked inventory. |
| 11 | **No fake or undisclosed persona** | No skill messages a stranger. `draft_negotiation` and `inquiry` **draft only** — Calvin sends. Every message a human receives is either sent by him or is plainly not a live conversation. |
| 12 | **Report only (self-audit)** | `skills/infra_recon.py` exposes no restart/patch/fix/deploy verb — the capability doesn't exist to be misused. It also scans **only enrolled targets**, so it can't port-scan a stranger by accident. |

---

## 3. High-level architecture

```
                         ┌──────────── clients ────────────┐
  laptop mic ── voice_client.py ──WSS──┐   phone ── Telegram voice/text ──┐   browser ── /dashboard ──┐   terminal ── manage.py
                                       ▼                                   ▼                           ▼                │
┌───────────────────────────── DigitalOcean droplet ──────────────────────────────────────────┐
│  kernel/                                                                                      │
│    app.py       FastAPI: /ws/voice · /api/command · /api/health · /api/voice · /api/session   │
│                 · /dashboard                                                                  │
│    registry.py  discovers skills/, registers contracts, dispatches, gathers scheduled jobs    │
│                                                                                               │
│  core/                                                                                        │
│    llm.py            NIM client + per-task router (model/key/endpoint/params)                 │
│    memory.py         PostgreSQL (psycopg 3, raw SQL), all tables + idempotent helpers        │
│    intent.py         keyword → LLM fallback intent router                                     │
│    config.py         merge .env secrets + config.yaml                                         │
│    skill.py          Skill interface + SkillContract + UNIVERSAL_INVARIANTS                   │
│    session.py        one cross-device session, turn history, cross-skill approvals            │
│    persona_store.py  PersonaEngine (answer-as-Calvin, facts, instructions, learning loop)     │
│    embeddings.py     pluggable embedder (NIM model | hashing)                                 │
│    doc_extract.py    pdf/pptx/docx/txt/md/image → text + chunking                             │
│    transcribe.py     faster-whisper wrapper (audio → text)                                    │
│    ats.py            CV keyword scoring + anti-fabrication check                              │
│    pdf.py            ReportLab PDFs (charcoal, no blue)                                        │
│    timetable.py      weekly class grid loader                                                 │
│    mailer.py         the ONLY email-send path (approval-gated applications)                   │
│    whatsapp.py       Africa's Talking WhatsApp/SMS push (flip alerts)                         │
│    spotify.py        Spotify Web API client (refuses endpoints dead for new apps)             │
│    notify.py         Telegram push (dependency-light)                                          │
│    gmail_client.py   Gmail API wrapper (labels/archive/recoverable trash/drafts; no send)     │
│                                                                                               │
│  skills/  (27, auto-discovered — see §8)                                                       │
└───────────────────────────────────────────────────────────────────────────────────────────────┘

  Four independently-restartable services (Phase 26):
    api      kernel + APScheduler — serves /api/*, ENQUEUES heavy work
    worker×N drains the job queue (scrape, score, tailor, transcribe, embed)
    bot      Telegram long-poll
    db       PostgreSQL — durable state AND the job queue

  Heavy work never runs in `api`, so a 6-hourly scrape or a 60s CV tailor cannot slow down
  the endpoint Calvin talks to. `docker compose up -d --scale worker=3` is safe because
  claims use FOR UPDATE SKIP LOCKED: N workers take N different rows, never the same one.
```

**Why this shape?** The kernel is tiny and never changes; all capability lives in skills that
are discovered at boot. The API process (with the scheduler) and the Telegram bot are separate
processes so one can crash/restart without taking the other down.

That separation is the deployment unit, and it survives either way of running it: **two PM2
processes** on a bare droplet, or **two containers from one image** under
[`docker-compose.yml`](../docker-compose.yml) (`docker compose restart api` provably leaves the
bot's uptime untouched). Docker is the recommended path — see
[DEPLOYMENT § 0](DEPLOYMENT.md#0-docker-the-recommended-path). Building api and bot as separate
images would let their dependencies drift apart, which is why they share one.

---

## 4. The kernel

**`kernel/app.py`** — a FastAPI app with a lifespan hook that, on startup:
1. discovers every skill (`registry.discover()`),
2. ensures the Postgres schema exists,
3. registers each skill's scheduled jobs with **APScheduler** (`Africa/Nairobi` tz),
4. starts the scheduler.

Endpoints (real login, `core/auth.py`, Slices 0a–0e — replaces the old shared
`AGENT_WS_TOKEN`; no code path reads that variable anymore):
- `POST /api/command` — guarded by `require_session` (an httpOnly `Secure` `SameSite=Strict`
  session cookie); `{text, spoken?, channel?}` → routes through the intent engine → skill →
  reply, and records the turn on Calvin's session. Used by the dashboard and any headless
  client (laptop voice client, a phone Shortcut) riding a device credential as that same
  cookie. The CLI dispatches locally and needs no session at all.
- `WS /ws/voice` — ticket-authed voice channel: `POST /api/auth/ws-ticket` (session-gated)
  mints a single-use ~15s ticket, since a browser cannot set an `Authorization` header on
  `new WebSocket()`. The reply includes the current `voice_id`+`rate` so the laptop client
  speaks with the selected pre-built voice.
- `GET /api/health` — scheduler / DB / NIM-key / Gmail-token / skills snapshot; liveness is
  public, the detail requires a session.
- `GET /api/voice` — the active pre-built voice + rate.
- `GET /api/session` — session-gated: the current session, recent turns, and pending approvals.
- `POST /api/auth/{password,webauthn/*,recovery}` — login; sets the session cookie.
- `POST /api/approvals/{id}/resolve` — the dashboard's path to `ApprovalStore.resolve()`
  (previously Telegram-only); demands a fresh passkey assertion in front of a `high`-tier
  approval when one is registered (`auth.step_up_on_high`).
- `GET /dashboard` — the browser channel (vanilla JS; no token or password ever touches
  `localStorage` or a JS variable — the session lives entirely in the httpOnly cookie).

**`kernel/registry.py`** — the seam that makes Principle 6 true:
- `discover()` imports every module in `skills/` and registers any `SKILL` instance (or
  `get_skill()` factory). A module with neither is skipped (that's how `telegram_bot.py` — a
  *process*, not a dispatchable skill — stays out of the registry). A skill that fails to
  import is logged and skipped; the kernel stays up.
- `dispatch_intent(intent)` runs the target skill's action; if the skill isn't built yet it
  degrades to a friendly "not wired up" message instead of crashing.
- `handle_command(text)` = intent router → dispatch.

**Intent routing** (`core/intent.py`) is two-stage: a fast **keyword/regex** table first
(zero latency, offline-testable), then an **LLM single-label classify** fallback only if no
keyword matches. Intents map to `(skill, action, args)`.

---

## 5. `core/` — shared infrastructure

- **`config.py`** — one cached `Settings` object merging `.env` secrets and `config.yaml`.
  Modules read config through this rather than `os.environ`. The exceptions are credentials that
  aren't on `Settings` at all and are read from the environment at their point of use: the
  per-task NIM keys (`core/llm.py`), Africa's Talking (`core/whatsapp.py`), and Spotify
  (`core/spotify.py`) — the Spotify refresh token deliberately so, since it must never reach the
  database. They resolve because importing any of these pulls in the logging setup, which builds
  `Settings` and thereby loads `.env` into the environment.
- **`llm.py`** — see [§6](#6-model-routing--concurrency).
- **`memory.py`** — a thin wrapper over one PostgreSQL connection (psycopg 3, raw SQL, **no
  ORM**). Writes are idempotent (`ON CONFLICT DO NOTHING` / status transitions). A `tx()`
  context manager + re-entrant lock keeps multi-statement transactions atomic across the
  api/bot threads, and an additive `_migrate()` (`ADD COLUMN IF NOT EXISTS`) evolves older
  databases without dropping anything. A `schema` argument namespaces all tables — the test
  suite uses it for isolation. See [§7](#7-data-model-postgresql).
- **`persona_store.py`** — `PersonaEngine`: facts CRUD, keyword retrieval, `answer()` (facts-only,
  returns `NEEDS_INPUT` on gaps), standing instructions, STAR story bank, weekly style profile,
  and the nightly `distill_edits()` learning loop that turns Calvin's edits into unverified
  candidate facts for confirmation.
- **`embeddings.py`** — a pluggable `Embedder`. `get_embedder("auto")` now tries
  **`NimEmbedder`** first (NIM-hosted `nvidia/nv-embedqa-e5-v5`, 1024-dim — switched from
  `baai/bge-m3` during Phase 37b after discovering that model returns a bare 500 on every
  call to this account's NIM endpoint, meaning semantic recall had been silently running on
  the hashing fallback), falling back to the
  dependency-free `HashingEmbedder`. The NIM route exists because the droplet has 961 MB of RAM
  and one CPU, which makes a local `sentence-transformers` model impossible rather than merely
  slow — the embedding runs where the compute is. The hashing embedder uses a **stable** hash
  (`hashlib`), not Python's per-process-salted `hash()`, so persisted vectors survive restarts.
  ⚠️ The test suite pins the hashing embedder via an autouse fixture: with `auto` resolving to
  NIM, any test touching recall would otherwise make a real network call and break the
  guarantee that `pytest` needs no API keys.
- **`semantic.py`** — `SemanticMemory` over pgvector (Phase 33): `index()`, `search()`,
  `recall_text()`, with keyword fallback when the extension is absent. `MIN_RELEVANCE` is the
  load-bearing detail — **nearest-neighbour search always returns something**, so without a
  floor an unrelated fact comes back with total confidence for being least-unrelated.
- **`approvals.py`** — `ApprovalStore` and the tier model (Phase 30). `LEARNABLE_TIERS` covers
  `low` and `medium` only; `high` is **deliberately absent**, so anything sent in Calvin's name
  asks every time regardless of how often he has approved it.
- **`queue.py`** — the Postgres-backed job queue (Phase 26): `enqueue`/`claim`/`complete`/`fail`,
  a `@handler` registry, and `run_skill` dispatch. Claims use `FOR UPDATE SKIP LOCKED`, so
  scaling workers needs no extra infrastructure.
- **`expiry.py`** — `JobExpiry` plus `parse_deadline` (Phase 34). The parser anchors on a cue
  ("deadline", "apply by", "closes on"), never on a bare date, and resolves every ambiguity to
  `None`; expiry sets `status='expired'` and never deletes.
- **`selftest.py`** — service-by-service pytest runs reported to Telegram as each group finishes
  (Phase 28). Never fabricates a pass.
- **`time_context.py`** — timezone-aware "now", relative due dates, and **`VOICE`**: one tone
  appended to `runtime_truth()` so every generative call speaks the same way (Phase 31).
- **`doc_extract.py` / `transcribe.py`** — text extraction (with OCR) and audio transcription;
  both inject cleanly for offline tests, with the heavy libs imported lazily.
- **`ats.py`** — pure ATS keyword scoring and the anti-fabrication check.
- **`skill.py`** — the `Skill` interface plus `SkillContract`, `INSTRUCTION_CATEGORIES` and
  `UNIVERSAL_INVARIANTS` (Phase 20). A contract's `__post_init__` re-adds the universal
  invariants and rejects unknown categories, so a skill cannot silently widen its own scope or
  drop a §0 guarantee.
- **`session.py`** — `SessionStore`: the one cross-device session (Phase 19) — turn history,
  live skill, authoritative last channel, and the read-only cross-skill approvals view.
- **`gmail_client.py` / `mailer.py` / `notify.py` / `whatsapp.py`** — Gmail (read/label/archive/
  **draft only** — no send method exists on `GmailClient`), the single approval-gated application
  **sender** (`mailer.py`, separate on purpose), Telegram push, and Africa's Talking WhatsApp/SMS
  for flip alerts (WhatsApp's endpoint is account-specific, hence config-driven, with SMS fallback).
- **`spotify.py`** — the Spotify Web API client (Phase 22). Read its module docstring before
  adding an endpoint: it deliberately doesn't define the calls Spotify killed for new apps, and
  refuses them at the transport as a second line of defence.
  A 403 is **diagnosed, not guessed at**. Spotify distinguishes `"Insufficient client scope"`
  (the saved grant predates a capability we added — re-authorising fixes it) from a bare
  `"Forbidden"` (the *app* may not call that endpoint at all — no re-auth will move it), and
  these need opposite responses. Measured against Calvin's live account: all scopes granted,
  `product=premium`, reads 200, and **playlist creation still returns bare "Forbidden"** — an
  app-level restriction to resolve in the Spotify Developer Dashboard, not a code or
  permissions problem. The old message offered "not Premium, or missing scope", both wrong.

---

## 6. Model routing & concurrency

This is central to how AgentOS uses the *best* model for each job and stays fast under
concurrent load.

### Task classes

Every LLM call declares a **task class**, not a model:

| Task class | Used for | Default model |
|------------|----------|---------------|
| `classify` | inbox classification, job categorisation, intent fallback, answer-judging | `meta/llama-3.1-8b-instruct` (cheap, high-volume) |
| `write` | cover letters, notes, digests, plans, drafts | `mistralai/mistral-medium-3.5-128b` |
| `persona` | answering as Calvin | `mistralai/mistral-medium-3.5-128b` |
| `code_review` | code tutor review, drill/lab grading | `deepseek-ai/deepseek-v4-pro` |
| `research` | web synthesis, interview prep, vault answers | `nvidia/nemotron-3-super-120b-a12b` |
| `voice_chat` | spoken chit-chat | `meta/llama-3.1-8b-instruct` |

Skills call `llm.chat("write", …)` / `llm.chat_json("classify", …, schema)` / `llm.classify(…)`.
No model id is ever written in a skill — swapping a model is a one-line `config.yaml` change.

### Per-task keys, endpoints, and params

A route in `config.yaml → llm.routes` can be a bare model string **or** a dict:

```yaml
routes:
  code_review: { model: "deepseek-ai/deepseek-v4-pro", api_key_env: NVIDIA_API_KEY_CODE }
  research:    { model: "nvidia/nemotron-3-super-120b-a12b", api_key_env: NVIDIA_API_KEY_RESEARCH, max_tokens: 1500 }
```

`resolve_route(task)` (in `core/llm.py`) returns a `Route(model, api_key, base_url, params)`:
- **`api_key_env`** — the env var holding that task's key. **If unset (or blank), it falls back
  to `NVIDIA_API_KEY`.** So a single key works out of the box; you add per-task keys only when
  you want to.
- **`base_url`** — a per-task endpoint (still NIM-compatible; free-first stays intact).
- **`temperature` / `max_tokens`** — per-task generation defaults; a per-call argument overrides them.

### Why per-task keys matter for concurrency

AgentOS runs work concurrently: the **APScheduler** fires jobs (hourly email cleanup, 6-hourly
hunts, the 07:00 briefing, the 15-min interview watcher…), the **Telegram bot** handles
commands, and a **voice** request can arrive — all at once, and all hitting the LLM.

With one key, those calls share one rate-limit bucket and can throttle each other. By giving
each *task class* its own key (`NVIDIA_API_KEY_FAST`, `_WRITE`, `_CODE`, `_RESEARCH`), a burst
of cheap `classify` calls (inbox cleanup) can't starve a `research` call (interview prep),
because they're on separate buckets. The `.env.example` documents the four suggested keys;
you can obtain multiple free NIM keys or point a task at any NIM-compatible endpoint.

### Reliability

`core/llm.py` retries on `429`/`5xx` with exponential backoff (honouring `Retry-After`),
`classify()` is strict-single-label with a lenient fallback, and `chat_json()` strips code
fences and makes one repair attempt before raising. Skills catch `LLMError` and degrade
(heuristic scoring, `NEEDS_INPUT`, "couldn't do that right now") rather than crash.

---

## 7. Data model (PostgreSQL)

One database (`DATABASE_URL`), raw SQL via psycopg 3 — no ORM, by deliberate convention.
Postgres (rather than an embedded file DB) matters because `agentos-api` and `agentos-bot`
are separate processes writing concurrently; a real server handles that without the
single-writer contention a file DB hits. Key tables:

| Table | Purpose |
|-------|---------|
| `jobs` | scraped jobs: score, category, cover, apply route, cv_variant, `deadline`, status (`new→scored→drafted→notified→approved→applied`/`skipped`/`expired`). `deadline` is parsed from the posting and is normally NULL — most postings never state one, and those fall back to the staleness rule (Phase 34) |
| `applications` | applied jobs: status (`applied→replied→interview→offer/rejected`), cv_variant_used |
| `emails` | processed emails: category, action (archived/labelled/drafted/trashed/restored) — idempotent by `gmail_id` |
| `persona_facts` | verified facts about Calvin by category; `stories` category = STAR anecdotes |
| `standing_instructions` | behaviour rules Calvin gives the agent (soft-deleted, never removed) |
| `cv_facts` | structured master-CV facts, versioned; cross-checked against `persona_facts` |
| `qa_log` | (draft, edited) pairs → the persona learning loop |
| `vault_files` / `vault_chunks` | course-note files (hash-idempotent) + embedded chunks (float32 `BYTEA`) |
| `flashcards` | SM-2 cards: ease/interval/due/lapses/status (`candidate→active→suspended`) |
| `card_reviews` | review log → retention stats |
| `deadlines` | CAT/assignment/exam/lab: due, weight, status (`active/pending/done/cancelled`) |
| `events` | free events: format, date, tags, status (`new→notified→interested→skipped`) |
| `kv` | generic key-value: session state, tag overrides, style profile, cv version, music session, etc. |
| `job_queue` | Phase 26 work queue: kind, JSON payload, status, attempts, backoff `run_at`, dedupe key, worker, last error. Claimed with `FOR UPDATE SKIP LOCKED`; failed rows are kept for inspection and requeue, never deleted |
| `listings` / `scores` | sourced deals + their hard-filter and quality scores (Phase 16) |
| `pipeline_state` / `pipeline_transitions` | the flip state machine + an **immutable** transition log |
| `negotiation_threads` | drafted (never sent) negotiation messages |
| `resale_listings` / `buyers` | cross-posted resale copies (views/inquiries) + committed buyers |
| `purchase_gate_checks` | the three-way gate that must pass before any purchase (Phase 16) |
| `margin_ledger` | one row per flip: buyer paid − seller price − fees; expired/rejected logged too, so hit-rate stays honest (Phase 18) |
| `sessions` | the single cross-device session: turn history, live skill, last channel (Phase 19) |
| `signal_log` | observed patterns: running_count, contradicted, status (`watching→proposed→confirmed/declined`) (Phase 20) |
| `skill_contracts` | each skill's declared `reads_categories` + `hard_invariants`, written at discovery (Phase 20) |
| `infra_scan_results` | findings: severity, occurrences, status (`open→resolved`) (Phase 21) |
| `semantic_index` | pgvector embeddings (`vector(1024)`, HNSW cosine) over facts, notes and CV material — retrieval instead of context-stuffing (Phase 33). Created **outside** the main schema transaction, so a missing `pgvector` extension degrades to keyword fallback rather than rolling back every table |
| `action_permissions` | learned answers per (skill, action, tier): `always_approve` / `always_deny` / `ask`. `high` tier is **never** learnable (Phase 30) |
| `pending_actions` | proposed actions awaiting Calvin's reply, with their tier and expiry; stale proposals expire rather than surfacing days later as a surprise (Phase 30) |
| `contacts` | phone book: `phone_e164` (normalized on write), retired not deleted (Phase 36) |
| `url_favourites` | named URL shortcuts opened laptop-side; http/https only (Phase 36) |

Conversational session state (mock interview, quiz, tutor) lives in `kv` as JSON, so it
survives restarts and is shared across voice/Telegram/dashboard/CLI. Phase 19's `sessions`
table sits above that: it holds the turn history and last-used channel for **one** session
keyed to Calvin — never to a device — which is what makes hand-off work.

Job rows are **retired, never deleted** — `status='expired'` drops a job from the queue and the
briefing while keeping the scrape, score and draft, so a wrong expiry heuristic stays
falsifiable (§0 Principle 4).

Note `standing_instructions` gained a **category** (Phase 20). A rule is only visible to a
skill whose contract declares it reads that category — a tone rule can never reach into the
code tutor's grading.

---

## 8. The skills, phase by phase

Each skill implements the `Skill` interface (`name`, `commands()`, `scheduled_jobs()`,
`handle()`) and exposes a module-level `SKILL`. "How achieved" notes the key mechanism.

### Phase 1 — Kernel & core
Foundations above. **Achieved:** FastAPI + APScheduler kernel, package-discovery registry,
raw-SQL Postgres, two-stage intent router, per-task LLM router.

### Phase 2 — Email agent (`email_agent.py`)
Hourly inbox cleanup classifies each new message (6 categories) and archives promo/newsletter/
social under `AgentOS/<Category>` labels; reply drafting creates a Gmail **draft**.
**Achieved:** `gmail.modify` scope (can't permanently delete); `GmailClient` has **no send
method** at all — replies are structurally draft-only. Idempotent by `gmail_id`.
**Note:** the standalone 07:00 digest was superseded by the Phase-13 morning briefing.

### Phase 3 — Job hunter (`job_hunter/`)
Modular scraper registry (`sources/`): RemoteOK/Remotive/Jobicy (JSON), a generic RSS source
(WWR, Himalayas, MyJobMag KE, CNCF…), transcription portals (notify-only), watched-company
deep-crawl, optional SerpAPI. Pipeline: scrape → dedupe → **category-aware score** → for
keepers, 2-line summary + cover email from verified facts → digest with Apply/Skip/Tailor
buttons → approval sends via `mailer` with the (tailored) CV → tracks applications.
15-min **interview watcher** matches inbound mail to applied companies and auto-fires a prep
pack. **Achieved:** polite `Fetcher` (UA, ≥2s/host, robots.txt, backoff); scoring via
`classify`-class `chat_json`; the send path is the separate approval-gated `mailer.py`.

### Phase 4 — Persona engine (`persona.py` + `core/persona_store.py`)
`answer(question)` retrieves relevant verified facts and answers *as Calvin*, first person —
or returns **`NEEDS_INPUT`** with the specific gap, never a guess. `/remember` `/forget`
`/instructions` manage standing rules that every skill can consult. A nightly job distills new
facts from Calvin's edits (stored **unverified** until he confirms). **Achieved:** facts-only
prompt + a short-circuit when nothing is retrieved + fail-safe to `NEEDS_INPUT` on LLM error.

### Phase 5 — Form & interview assistant (`form_assist.py`)
Parses a form/screener (LLM + heuristic fallback), answers each question from the KB, pulls
behavioral questions from the **STAR story bank** (or prompts to build one), and **refuses to
solve skills tests** (routes them to a prep pack). Produces a numbered answer sheet; `submit()`
is approval-gated. **Achieved:** per-question status (`answered/needs_input/story_needed/
assessment_skipped`); assessments detected by regex independent of the model's own label.

### Phase 6 — Research + interview prep (`skills/research/`, `interview_prep.py`)
`research()` does a free DuckDuckGo search, synthesizes a **cited** answer, and never invents
sources — kept as a fast, snippet-based grounding helper for OTHER skills (interview prep's
company research, study vault's web fallback below). Prep packs research the company then
generate 15 Q&A (in Calvin's voice), 3 questions to ask, and a checklist → a charcoal **PDF** +
Telegram summary. `/mock` is a kv-backed one-question-at-a-time rehearsal with candid feedback.
**Achieved:** injectable searcher; PDF via `core/pdf.py`. **The user-facing `/research` command
itself was rebuilt in Phase 39 below** — a request now always produces a full house-styled PDF
report (authoritative sourcing, cited synthesis, images, references), never `research()`'s
plain cited-text reply.

### Phase 7 — Voice layer (`voice.py`, `client/`)
Laptop client: wake word (openwakeword) → record-till-silence → local faster-whisper STT →
authed WSS → spoken reply via **edge-tts using only pre-built neural voices**. Barge-in +
push-to-talk. `voice.py` manages voice selection + rate + mute; it **refuses any voice not in
the stock registry** (no cloning). **Achieved:** `client/voice_utils.py` holds the
hardware-free logic (tested); a codebase-wide guardrail test forbids cloning imports.

### Phase 8 — Telegram bot (`telegram_bot.py`)
Full remote control from one authorized chat: all commands, **inline buttons** (Apply/Skip/
Tailor, quiz grading, card approval, deadline confirm, event interest), free text → the same
intent router, and **voice notes** transcribed on the droplet and routed identically.
**Achieved:** `BotCore` holds all testable logic; the python-telegram-bot handlers are thin
async wrappers. It's a standalone PM2 process (no `SKILL` export → not in the registry).

### Phase 9 — Study vault (`study_vault.py`)
Drop notes in `data/vault/<UNIT>/`; ingestion extracts text (OCR images), chunks ~800 tokens,
embeds locally, stores vectors in Postgres (`BYTEA`). `ask(question, unit)` retrieves top-k and answers
**always citing file + page/slide**; below a confidence floor it says **"not in your notes"**
and offers a clearly-labelled web answer. **Achieved:** pluggable embedder; content stays
local (only retrieved chunks go to NIM).

### Phase 10 — Lecture capture (`lecture_capture.py`)
Audio → faster-whisper transcript → cleanup (filler removal, Swahili/English code-switch fixes)
→ structured notes + **"examinable signals"** + 10–20 **candidate flashcards** → charcoal PDF →
transcript auto-ingested into the vault. Processed audio is **moved** to `processed/` (idempotent).
**Achieved:** injectable transcriber; flashcards enter as `candidate` (await approval).

### Phase 11 — Spaced repetition (`spaced_rep.py` + `core/sm2.py`)
Pure SM-2 scheduling. Approve/edit candidate cards, then `quiz`/`/quiz` sessions: Telegram
Again/Hard/Good/Easy buttons, or voice mode where the LLM **judges** the spoken answer (lenient
on phrasing, strict on substance). Weekly retention report; **surge** brings a unit's weak cards
forward before an exam. **Achieved:** session in `kv`; `core/sm2.py` is clock-free and fully tested.

### Phase 12 — Code tutor (`code_tutor.py`)
Five modes: **explain** (C++ pointer-level examples), **review** (code_review model, teaching
not a rewrite), **drill** (problem ladder; wrong answer → flashcard candidate), **socratic**
(guiding questions only; "just tell me" escape hatch), **mock lab** (timed, rubric-graded →
weak topics become flashcards). **Refuses to solve live CTFs / graded assignments.**
**Achieved:** session in `kv`; `_LIVE_CTF_RE` guard; free text continues the active mode.

### Phase 13 — Semester command center (`semester_planner.py`)
The integrative hub. `deadlines` table (email-extracted dates confirmed before saving) +
`config/timetable.yaml` drive the **unified 07:00 briefing** (classes, deadlines ranked by
urgency×weight, cards due, job approvals, interviews, events, commitments, LLM top-3) — which
**replaces** the plain inbox digest. `/plan` (week planner) and `/cram` (surge weak cards +
revision schedule + **MUST-format mock CAT PDF**, marking scheme withheld until attempted).
**Achieved:** pulls from every other skill's data via `memory`.

### Phase 14 — Event scout (`event_scout/`)
Free events matching editable interest tags (CTFtime real API + config RSS/ICS feeds). Free-only
(paid **dropped**), deduped, ranked by tag-match then date, physical events biased to Nairobi/
Mombasa. `/tags` edits interests; **Interested** promotes an event into the planner so it shows
in the briefing. **Achieved:** reuses the polite `Fetcher`; weekly digest + <48h closing push.

### Phase 15 — CV tailoring & ATS optimization (`cv_tailor.py` + `core/ats.py`)
One verified `data/cv/master_cv.*` → structured `cv_facts` (diff + persona cross-check). Per job:
an ATS-optimized variant that **only reorders/emphasizes/rephrases what's already true**, mirrors
JD terminology where genuine, shows an **ATS keyword-match score before→after**, and **flags gaps
instead of inventing**. Saved to `data/cv/variants/` (never overwrites the master) and linked to
the job so the hunter attaches it on approval. **Achieved:** `core/ats.fabrication_terms()`
catches any tech term the draft adds that the master doesn't support.

### Phases 16–18 — Flash-flip deal broker (`deal_broker/`)
One pipeline built as three phases. **16 — sourcing & negotiation:** `sources.py` pulls listings
from Jiji (JSON) and Pigiame (HTML); `scoring.py` is pure (hard filters — ≥20% below the comp
median, ≥14 days stale or a motivated seller, category velocity ≤7 days — then a quality rank
that must clear 60). `skill.py` is a state machine: `DISCOVERED→SCORING→NEGOTIATING→LISTED→
BUYER_FOUND→PURCHASE_GATE→PURCHASED→DELIVERED`, plus `EXPIRED`/`REJECTED` off-ramps, enforced by
a `TRANSITIONS` table. **17 — resale:** one item is cross-posted to every configured platform
under **one shared flash window and price**; the instant a buyer commits, `delist_others()` fires
and a second commitment is refused. Price drops through tiers `[0.9, 0.8]`, then expires.
**18 — margin ledger:** opens a position at `LISTED`, closes it when the gate approves
(`buyer_paid − seller_price − fees`), and logs expired/rejected attempts as zero-margin so
`hit_rate` isn't flattering. Margin is on **revenue**, matching Phase 16's `price_gap_pct`.

**Achieved — the two capital-risk rules are structural, not policy:** there is **no payment
integration** in the codebase, so `confirm_purchase` cannot spend; it refuses unless
`has_passing_gate()` (committed buyer **and** re-confirmed availability **and** Calvin's
approval). And there is **no seller/buyer messaging path** — `draft_negotiation` and `inquiry`
only draft; Calvin sends. **Facebook Marketplace is deliberately not scraped** (Meta actively
pursues scrapers); sourcing sticks to lower-risk Kenyan platforms. Alerts go out over Africa's
Talking (`core/whatsapp.py`) — WhatsApp when an approved sender is configured, SMS otherwise.

### Phase 19 — Cross-device continuity (`session.py` + `core/session.py`)
**One** session, keyed to Calvin — never to a device. `SessionStore` records every turn from any
of the four channels (telegram | voice | dashboard | cli, last 12 turns), tracks the live skill
state machine, and exposes `pending_approvals()` — a **read-only** cross-skill view of everything
waiting on him (jobs, flips, flashcards, deadlines, proposed rules). `handoff_summary()` catches
him up when he switches devices. **Achieved:** `sessions.active_skill` is the *live state machine*,
deliberately distinct from `turn.skill` (who routed the last turn) — conflating them made hand-off
report the wrong thing. `last_channel` is **authoritative**: a claim that contradicts it gets a
warning rather than belief. Phase 19 also added the fourth channel, `/dashboard` (vanilla JS,
Bearer-authed).

### Phase 20 — Adaptive behavior layer & Skill Contracts (`adaptive.py`)
The layer that lets AgentOS change **with** Calvin without changing **itself**. Logging is
**passive**: a signal never alters behaviour on its own. A pattern must repeat `threshold` (4)
times with **zero contradicting instances** — one counter-example disqualifies it — before it's
*proposed*. Only Calvin turns a proposal into a rule; a declined pattern is never proposed again.

**Skill Contracts** bound the whole thing. `core/skill.py` defines nine `INSTRUCTION_CATEGORIES`
and five `UNIVERSAL_INVARIANTS` (`approval_gate`, `never_delete_data`, `never_fabricate`,
`no_face_cloning`, `no_voice_cloning`) that are **re-added to every contract even if a skill
omits them**. `BaseSkill.contract()` defaults to reading **nothing** — silence means no rule
reaches you, which is the safe default. The registry writes contracts to `skill_contracts` at
discovery. Instructions outside a skill's declared scope are **ignored, not applied**, and
`violates_invariant()` refuses any proposed rule that would switch off a §0 guarantee.
**Achieved:** the codebase-wide cloning guardrail test skips lines containing an enforcement
marker — `adaptive.py`'s tripwires must name `no_voice_cloning` in order to refuse it — with a
companion test proving the scan still catches a real offender.

### Phase 21 — Self-audit / infra recon (`infra_recon.py`)
A weekly (Sun 06:00) security pass over Calvin's *own* footprint — his CTF/security interest
pointed at his own infrastructure. Checks: open ports vs. expected, TLS expiry (`tls_warn_days`),
sensitive files reachable over HTTP (`/.env`, `/.git/config`, …), CVEs in our **declared**
dependencies via **OSV.dev** (free, keyless), and PM2/Docker container health. Findings are ranked by severity;
one that persists across `ESCALATE_AFTER` (3) scans **escalates** rather than repeating
identically forever, and a fixed one resolves itself (status change — never a delete, §0 P4).

**Achieved:** *report only* is structural — no `restart`/`patch`/`fix`/`deploy` verb exists on
the skill, so a false positive can never take a live service down. *Enrolled targets only* means
it scans nothing Calvin hasn't explicitly enrolled. Its contract declares
`reads_categories == []` **on purpose**: no standing instruction may steer a security scan. HTTP
probing is skipped entirely unless 80/443 is open — the port scan already answered that, and
asking anyway costs a full retry-and-backoff cycle per path.

The CVE check takes package **names** from `requirements.txt` but their **versions from
`importlib.metadata`** — what's installed and running. OSV needs an exact version, and reading it
from the manifest only works if every line is `==`-pinned; ours are ranges, which meant the check
quietly queried nothing at all. The installed version is exact rather than guessed, and it's the
one that can actually be exploited. A requirement that isn't installed is skipped, not assumed.

**Known gap:** this covers the ~21 packages we *declare*, not the ~190 in the environment.
Transitive dependencies — `urllib3`, `cryptography`, `lxml`, `pydantic` and friends — are never
queried, and that's where a lot of real CVEs live. Widening it to every installed distribution
is a one-line change (`importlib.metadata.distributions()`); it's left narrow deliberately for
now, because a weekly report about a package Calvin can't directly upgrade is noise he'd learn
to skim. Revisit if the direct-dependency scan proves too quiet.

### Phase 22 — Music companion (`music.py` + `core/spotify.py`)
Honest about a restricted API. Spotify removed **Recommendations, Audio Features, Audio Analysis,
Related Artists and Featured Playlists for new apps**, and the Web API **cannot mix audio at any
tier**; the account must be **Premium**. So: the taste model is built from what *does* exist
(recently played, top tracks/artists over three ranges, saved library); song choices come from
the model's own knowledge and are then **resolved to real tracks via Search**, so nothing is
suggested that wasn't verified to exist; and "DJ mode" is **smart sequencing plus narrated
transitions**, not beatmatching. Discovery is framed as AgentOS's suggestion with a one-line
"why" — **never** as Spotify's recommendation, because it isn't.

**Achieved:** that honesty is enforced in code — `core/spotify.py` doesn't define the dead
endpoints at all, and `_call()` refuses them anyway as belt-and-braces. Those patterns are
compiled to a regex (`{id}` → `[^/]+`); compared literally, `/artists/{id}/related-artists`
would never match a real artist id and the guard would silently pass. Narration uses a **stock
edge-tts voice** (§0 P9). Standing rules are evaluated **per rule** — joining them lets one
rule's time window leak onto another — and `_local_hour()` resolves through `zoneinfo` +
`settings.tz`, because "before 8am" means 8am in **Nairobi**, not on a UTC droplet.

### Phase 23 — Desktop app control (`desktop.py` + `client/apps.py`)
Open, close and focus apps on Calvin's laptop by voice. The droplet **cannot reach the laptop**
— the voice client opens a WebSocket per utterance and closes it — so this rides the reply:
`skills/desktop.py` resolves an utterance to an app **key** and returns
`data["client_actions"]`, the kernel puts `{"op", "app"}` on the `/ws/voice` reply, and
`client/apps.py` executes it against `client/apps.yaml`. Only the voice channel carries actions;
Telegram and the dashboard have no route to the laptop.

**Achieved — the security model is the design.** The server emits an app **key, never a
command**: it is internet-facing and LLM-driven, so if it could name a binary, a leaked
`AGENT_WS_TOKEN` would be remote code execution on the laptop. `client/apps.yaml` is the real
allowlist and re-checks every key, so the worst a compromised droplet can do is ask for an app
Calvin already approved; the kernel additionally narrows the wire to `op`+`app`, so a buggy
skill can't smuggle an argv through. Commands are **argv lists run with `shell=False`** —
server input is only ever a dict key, never interpolated. **There is no force-kill op**:
`close` posts WM_CLOSE / `quit` / SIGTERM so an editor can still prompt about unsaved work,
because losing it is data loss and §0 P4 has no "it was only a text file" exception. Standing
rules in the new `desktop` category can *remove* capability ("never close vs code") but never
grant it. App-name matching requires the key's words to be **contained in** what was said
(`code` ⊆ "vs code"), never the reverse — substring matching would resolve "code" to
`vscode_insiders` and launch the wrong thing — and ambiguity is refused rather than guessed.

This is also what rescues Phase 22: the Web API cannot start Spotify, so `"No active Spotify
device — open Spotify somewhere first"` was a dead end for someone talking to the agent with
their hands full. `music.play()` now returns an `open spotify` action on that one error (and
only that one — a 403 for a non-Premium account opens nothing), while still reporting
`ok=False`, because playback genuinely didn't start.

### Phase 24 — Desktop window (`client/agent_window.py` + `client/assistant_core.py`)
A tray window replaces the always-on wake word. **Achieved:** `AssistantCore` holds the whole
session (mic state machine, typing, actions, errors) with every dependency injected, so it is
tested without audio hardware; `agent_window.py` is a thin tkinter shell.
The property that justifies it: **the OS audio stream is OPENED on toggle-on and CLOSED on
toggle-off, on window close, and on any crash in the loop** — so Windows' own microphone
indicator is the truth, not a checkbox we drew. Typing works with the mic shut, which is the
point. Native rather than a browser page on purpose: the Web Speech API would ship Calvin's
voice to a cloud STT, destroying the privacy this design exists for. Whisper and edge-tts stay
on the laptop; only the transcript crosses the tunnel. The wake word survives behind
`AGENT_CLIENT_MODE=voice` — opt-in, never the default, because nobody opts *out* of a mic.

### Phase 25 — GitHub persona import (`core/github_profile.py`)
45 public repos are better evidence of what Calvin builds than any interview answer.
**Achieved:** profile/repos/READMEs/contributor-graphs → structured facts, plus a
`collaborations` list so work he did on *other people's* repos (UMS, Project47, ZKSentinel)
counts. Everything lands **unverified** and waits for him (§0 P5) — a machine reading a README
does not decide what is true about a person, and "experimenting with Kubernetes" must never
reach an employer as "uses Kubernetes". GitHub only: **LinkedIn is deliberately not fetched**,
the same call `config.yaml` already makes about Facebook Marketplace — their ToS forbids
automated access and it is *his* account that gets restricted.
The subtle part: reading each repo's *dominant* language hid every infrastructure signal
(GitHub reports `Dockerfile`, `PLpgSQL`, `Shell` only in the per-repo breakdown), which is why
a CV tailored for an SRE role once listed Docker as a **gap** for someone who ships Dockerfiles.

### Phase 26 — Job queue & worker service (`core/queue.py`, `kernel/worker.py`)
The api/worker split that makes AgentOS scale. **Achieved:** a Postgres-backed queue using
`FOR UPDATE SKIP LOCKED`, so `--scale worker=3` runs three jobs at once and two workers can
never claim the same row. Handlers register by **name** (`@handler("job_hunter.score_one")`),
so a queued row holds strings rather than a pickled callable and a worker on a newer image can
drain rows enqueued by an older one. Jobs carry attempts, exponential backoff, a last error and
a status; failures are **kept, never deleted** (§0 P4), inspectable via `manage.py queue` and
requeueable after a fix.
Why Postgres and not Redis/Celery: the database is already there, already backed up, already
shared by both processes — one less service to run is the point of splitting on real boundaries
rather than adding infrastructure.
**This deleted the 40-job cap as a concept.** `max_score_per_run: 40` and "(438 more deferred
to the next run)" were never policy — they were the absence of a queue, and because each run
also scraped fresh postings, 741 jobs sat unscored for days. The cap now bounds one *pass*; the
overflow is enqueued and drained, deduped by job id.
`ScheduledJob(queued=True, skill=…, action=…)` moves any heavy timer job off the API process
(hunt, lecture transcription, vault embedding, flip/event scrapes) without rewriting the skill —
the scheduler enqueues, a generic `skill.run` handler dispatches by name in the worker. Light
jobs stay inline: a 2-second no-op gains nothing from a queue hop.

### Phase 27 — Continuous music session (`music.start_session` / `session_tick`)
Music that keeps playing until told to stop, driven from the **server** so it survives the
laptop sleeping. **Achieved:** session state in `kv`, plus a 4-minute tick that tops the queue
up — under the length of most tracks, so it never runs dry. The droplet is the DJ, not the
stereo: audio comes out of whichever Spotify device is active.
Honest about the limits: Spotify has **no clear-queue API**, so `stop` says plainly that up to
`SESSION_LOOKAHEAD` already-queued tracks may still play rather than claiming silence. The tick
no-ops unless a session is active (a timer that acts unasked is how music starts by itself at
3am), and a device disappearing mid-session does *not* kill the session — he'll reopen Spotify
and expect it to resume.


### Phase 28 — Service-by-service self-test (`core/selftest.py`)
`pytest` already existed; the value here is that results arrive **on his phone, grouped by
service, as each group finishes** — a deploy can be verified from anywhere without reading a
wall of dots. "540 passed" tells you nothing about *which capability* broke.
**Never fabricates a pass.** A group that errors, times out, or whose runner falls over reports
❌ with the reason. Two bugs found the hard way are pinned by tests: passing `-q` when
`pytest.ini` already sets it makes `-qq`, which suppresses the very summary line the counts are
parsed from (every service reported a confident "✅ passed (0 tests)"); and scanning every line
containing "passed"/"failed" read numbers out of tracebacks — with the database unreachable one
run totalled *103,445 tests*. Parsing is now anchored on pytest's final summary line, which
always ends in a duration. A self-test whose numbers can't be trusted is worse than none,
because it is believed.

### Phase 29 — Listening budget (`music.budget`)
A monthly minutes cap (default 10k) for the continuous session, so "play music until I say
stop" can't quietly run all month. Spent minutes accrue on each `session_tick`; the session
stops itself at the cap rather than being silently throttled.

### Phase 30 — Tiered actions & learned permissions (`core/approvals.py`)
The approval gate used to be all-or-nothing, which trains you to rubber-stamp. Actions now
carry a **tier** — `trivial` (just do it) · `low` · `medium` · `high` — and Calvin's answers are
remembered per (skill, action, tier) as `always_approve` / `always_deny` / `ask`.
**`high` is deliberately absent from `LEARNABLE_TIERS`.** Sending something in his name, or
anything irreversible, asks *every time* no matter how often he has said yes. A permission
model that can learn its way to "never ask before sending" defeats the point of having one.
Replies parse naturally ("3 yes", "always no 3", "yes all") against the pending set, and stale
proposals expire rather than lingering as a surprise action days later.

### Phase 31 — One voice, and progress that shows (`core/time_context.py`, `telegram_bot.py`)
Adopted from the reference assistants Calvin pointed at. `VOICE` is appended to `runtime_truth()`
so **every** generative call carries the same tone — lead with the answer, plain English, say
plainly when something failed — rather than each skill inventing its own register.
Calvin: *"when i tell the bot to clear emails i need to see clearing emails in progress"*. Long
actions now acknowledge before they start (`🧹 Finding those emails…`, `🎵 Building that
playlist…`). Deliberately **text-pattern matched, not router-based**: an acknowledgement that
needs an LLM round-trip to decide what to say is not an acknowledgement. It is wrapped so a
courtesy line can never block or break the real reply.

### Phase 32 — Proactive triage (`skills/proactive.py`)
The loop that acts without being asked, on top of Phase 30's tiers. Scoped hard: `ACTION_KINDS`
is limited to `email_archive` / `email_trash` / `email_label`, and **the tier is read from our
own table, never from the proposed payload** — otherwise anything that can write a proposal can
mark itself trivial and bypass the gate.
Runs as a cron job at **05:30**, deliberately ahead of the 07:00 briefing, so the briefing
reports an inbox that has already been triaged rather than one still full of noise.
An out-of-vocabulary proposal is **dropped, not escalated** — that is the point of the fixed
vocabulary, since the model must not be able to widen its own remit by naming a new action.

### Phase 33 — Semantic memory (`core/semantic.py`, `core/embeddings.py`)
Calvin: *"set up vector databases instead of stuffing it with too much in context"*. pgvector
with an HNSW cosine index, embeddings from NIM-hosted **`nvidia/nv-embedqa-e5-v5`** (1024-dim)
— no local model, because the droplet has 961 MB of RAM and one CPU, which makes `sentence-transformers`
impossible rather than merely slow. Measured **~71% context reduction** on CV tailoring.
Originally shipped on `baai/bge-m3`; switched in Phase 37b after discovering that model
returns a bare 500 on every call to this account's NIM endpoint (confirmed not a request-shape
issue — `nv-embedqa-e5-v5` succeeds on the identical payload). Because `NimEmbedder` already
degrades silently to the hashing fallback on any failure, this had been running on lexical
hashing in production the whole time, with nothing anywhere surfacing it as broken.
Two deliberate choices: `MIN_RELEVANCE` exists because **nearest-neighbour search always
returns something** — without a floor, an unrelated fact is retrieved with total confidence
simply for being least-unrelated. And the vector table is created **outside** the main schema
transaction, so a missing `pgvector` extension degrades to keyword fallback instead of rolling
back every table in the system.

### Phase 34 — Expiry, deadlines, and the real posting (`core/expiry.py`, `job_hunter/enrich.py`)
Calvin asked twice; the queue had reached **83 drafted jobs** awaiting him. That is an attention
problem, not a storage one — a list nobody can read is a list nobody reviews.
Two rules: **stale** (pending >2 days without a decision) and **past deadline**. A job that is
both is reported as closed, since that is the reason that actually tells him something. The
first live sweep retired 49 of 91 pending jobs.
**Nothing is deleted** (§0 Principle 4): expiry sets `status='expired'`, which drops the job
from the queue and the briefing while keeping every scrape, score and draft, so a wrong
heuristic stays falsifiable. It never touches `approved`/`applied`/`skipped` — age is
meaningless once he has acted, and those represent a tailored CV or a sent application.
Deadlines needed building too (`jobs.deadline`, parsed at scrape time, with a backfill). The
parser anchors on a **cue** — "deadline", "apply by", "closes on" — never on any date in the
text, because postings are full of dates that aren't deadlines (start dates, founding years,
"since 2019"). Expiring a live role because it mentioned 2019 is far worse than missing a
deadline, so every ambiguity resolves to *no deadline* and falls back to staleness.
The briefing carries the other half: applications **closing within three days**, named
individually and soonest first. Expiry alone only ever delivers bad news.
**34b — enrichment.** The measurement that prompted it: across 42 pending jobs the median
description was **162 characters** and not one mentioned a deadline. The sources hand back
stubs; the real text is on the posting's own page, which we stored a link to and never opened.
Keepers now get one GET — *after* scoring, *before* the cover is drafted or the CV tailored —
through the shared `Fetcher`, so robots.txt, the 2s-per-host floor and the User-Agent are
inherited rather than reimplemented. Best-effort by design: a dead link or JS-only page leaves
the stub standing, and it only ever *upgrades* (some pages extract to less than the source
gave us, and trading a clean summary for a login wall is a silent downgrade).

### Phase 36 — JARVIS shell + device control (`frontend/`, `client/hud_window.py`,
`client/adb_bridge.py`, `skills/{contacts,web_open,youtube,weather,phone}.py`)
Two things bundled as one phase: a real HUD (one frontend, two shells) over the existing
kernel, and five capabilities that were genuinely missing — a phone book, outbound calls,
call pickup/hangup, URL opening, YouTube playback, and weather. Everything else the phase's
own build prompt asked for (voice, app launch, Spotify, chat, persona) already existed and
was wired to, not rebuilt.

**A — the frontend.** `frontend/` is the one source tree for both the browser dashboard
(`/dashboard`, mounted via `StaticFiles(html=True)` — no more single-file
`kernel/static/dashboard.html`) and the desktop HUD. Panels render conditionally on a
negotiated `capabilities` object (`{shell, local, adb, apps, mic}`); the desktop shell
detects itself via `?shell=desktop` on its own URL (see below) and awaits pywebview's
`pywebviewready` event before asking its bridge what it can actually do — a capability this
build hasn't wired up yet reports `false` rather than appearing and then failing (§0: never
fabricate a check that passed). No framework, no bundler: vanilla ES modules, one relative
import graph that works identically whether served over HTTP or loaded off a local disk.
**Achieved:** boot sequence streams real `/api/health` subsystem checks (a failed one stays
red, never silently passes); HUD states (idle/listening/thinking/speaking/
awaiting_approval/error) are driven by one `hud.js` state machine, with a ~200ms flash on an
actual *change* and never an ambient pulse at rest; `[hidden] { display: none !important }`
exists because a same-specificity author rule (`.boot-screen { display: flex }`) otherwise
silently beats the browser's own `[hidden]` rule — found by actually loading the page in a
browser, not by reading the CSS.

**Desktop shell (`client/hud_window.py`).** Extends `client/`, doesn't replace it —
`agent_window.py`'s tkinter window still works standalone. Same `AssistantCore` (Phase 24),
same `Microphone`/`Transcriber`/`Speaker`/`send_to_agent`/`run_actions` helpers from
`voice_client.py`. Two things worth knowing:
- **The HUD's HTML is served over a loopback HTTP server on an ephemeral port, never
  `file://`.** Chromium's WebView2 (pywebview's Windows backend) blocks relative
  `<script type="module">` imports and same-origin `fetch()` assumptions under `file://`;
  a loopback server keeps everything genuinely local (nothing leaves the laptop to fetch
  these files) while giving the page the real `http://` origin ES modules need. The window
  URL carries `?shell=desktop&server=<droplet-url>` so `transport.js` knows to point its
  actual API/WS traffic at the droplet instead of the static-file server that merely served
  the HTML.
- **The barge-in recorder used to be duplicated.** `agent_window.py`'s `_record` had a
  subtle adaptive-echo-baseline algorithm inline; rather than hand-copy it into the new
  window and risk the two drifting apart, it's now `client/audio_io.py`, taking the
  `AssistantCore`, frame queue, and timing constants as arguments. Both windows call it with
  the exact same `voice_client.py` constants; a test (`test_audio_io.py`) exercises it with
  a fake queue and a fake core, no audio hardware required.

**Global hotkey / tray / compact mode.** `pynput.keyboard.GlobalHotKeys` (default
`<ctrl>+<alt>+j`) summons/dismisses the window without root on Windows/macOS/X11 — Wayland
has no portable global-hotkey API, and this is *reported*, not silently swallowed. The tray
icon mirrors `agent_window.py`'s (`pystray`, "Show"/"Hide"/"Quit"). Compact mode resizes and
repositions the actual OS window (`screeninfo` for multi-monitor placement, falling back to
a 1920×1080 guess rather than crashing) **and** toggles `[data-compact]` in the same call —
the CSS collapse and the window shrink can never happen one without the other. The frontend
expands out of compact automatically on any HUD activity (a `bridge:core` push with a
non-`off` state); collapsing back is left to the hotkey, not an idle timer, so a quiet HUD
never surprises anyone by shrinking mid-read.

**Wake word, opt-in only.** Behind `AGENT_CLIENT_MODE=voice` (same gate Phase 24 already
uses — "nobody opts *out* of a hot microphone"). The detector runs its own mic stream only
while `AssistantCore.mic_on` is false, closing it the instant a real session starts so it
never contends with `AssistantCore`'s own stream for the device; detection shows the window,
expands it, and calls `core.mic_on_()` — the *same* always-listening-once-activated session
Phase 24 already has, not a second recording pipeline.

**B1 — Phone book (`skills/contacts.py`, `contacts` table).** Numbers normalize to E.164 via
`phonenumbers` (offline metadata, no network call) against `phone.default_region`
(`config.yaml`, default `KE`) — there is no hardcoded country code anywhere in this phase.
Un-normalizable input is rejected before it reaches the table. Lookup is fuzzy on name but
**returns every match**; one match proceeds, zero says so, more than one asks which — this
module never indexes `[0]`. Retire, never delete (§0 P4): `retired_at NULL` means active.
CSV and vCard import are both idempotent (same name + same, possibly absent, number = the
same contact already on file) and a minimal vCard reader (`FN`/`TEL`/`EMAIL`/`NOTE` only —
not a full RFC 6350 parser; a contact it can't read is skipped, not guessed at). Contacts
are PII: lookups log a contact **id**, never a name or number.

**B2/B3 — Calls, pickup, hangup (`skills/phone.py` + `client/adb_bridge.py`).** Placing a
call previews the resolved name and full E.164 number and is `high` tier — the same
draft-then-confirm shape `email_agent.py`'s `compose`/`continue_send` already uses (a kv
session, re-entered one-shot via `kernel/registry.py`'s `_active_continuation`), not the
`ApprovalStore` proposal queue (that's for the proactive/background-triage case). `high`
sits outside `LEARNABLE_TIERS` structurally (Phase 30) — there is no code path, correct or
buggy, that can teach it "always yes"; `continue_call` is excluded from the planner's
manifest (`_PLAN_INTERNAL_ACTIONS`) so it can never be picked as a freestanding step. The
server only ever emits an app-key-shaped action — `{"op": "call", "number": "+254..."}` /
`{"op": "answer"}` / `{"op": "hangup"}` — re-validated against strict E.164 a second time at
`kernel/app.py`'s `_client_actions` narrow waist before it reaches the wire, mirroring
Phase 23's "the server never gets to override the laptop's allowlist" rule exactly.
`client/adb_bridge.py` re-validates a third time independently (its own `E164_RE`, deliberately
not imported from the server side — server and laptop stay independently deployable, Phase
26) and only then builds an **argv list**, `shell=False` always, nothing built by string
interpolation even after validation. Device selection is explicit: no device, an
unauthorized device, and more than one connected device are each a clean `Outcome`, never a
traceback and never a guess at which phone to dial from. Answering (`KEYCODE_HEADSETHOOK`
79, falling back to `KEYCODE_CALL` 5) and hanging up (`KEYCODE_ENDCALL` 6) are `low` tier and
learnable — they act on a call that already exists, not one initiated in Calvin's name.
**There is no force-terminate op anywhere in this module** (`test_no_force_kill_path_exists`
asserts the class's entire public surface). Call-state polling (`dumpsys
telephony.registry`) is one-shot and caller-driven — nothing here starts its own background
poll loop.

**B4 — Open URLs (`skills/web_open.py`, `url_favourites` table).** Scheme allowlist is
`http`/`https` only, checked in the skill **and again** at the same `_client_actions` narrow
waist that re-checks calls — belt and braces, the pattern `core/spotify.py`'s dead-endpoint
guard already established. `file:`, `javascript:`, and anything else are refused outright,
with no per-instruction override. Execution is laptop-side via Python's `webbrowser` module
(cross-platform — not `os.system('start ...')`).

**B5 — YouTube (`skills/youtube.py`).** No key needed by default: the query resolves to a
YouTube *search* URL (opened via B4), and YouTube itself picks the result — nothing here
scrapes a page or guesses a video id. `YOUTUBE_API_KEY` (optional) resolves the top hit via
the Data API and opens the video directly; absent the key, or on any API failure, this falls
back to search silently rather than guessing (§0 P5). Filler-stripping layers on top of
`core/intent.py`'s existing punctuation normalization rather than a second one, mirroring
`skills/desktop.py`'s established filler-word pattern for the verbs/words `core/intent.py`
itself doesn't touch.

**B6 — Weather (`skills/weather.py`).** Open-Meteo, keyless and free (§0 P1), for both
geocoding and forecast — no paid provider considered. Default location comes from a
verified `city`/`location` persona fact; "weather in Mombasa" overrides it for one call.
Cached 30 minutes per location in the existing `kv` store. A network failure degrades to
"I couldn't reach the weather service" — never an invented forecast; verified live against
the real API during this build (`weather in Nairobi` → a genuine Open-Meteo fetch, not a
mock). The voice sentence and the HUD panel are built from the **same** fetched dict, so
they can't disagree about what was actually retrieved. Folded into the 07:00 briefing
(`semester_planner.py`) rather than left standalone — a second try/except there means a
weather-service hiccup can never take the whole briefing down with it.

**Intent routing.** Six new keyword rules (`weather`, `youtube_play`, `contacts_find`,
`phone_call`, `phone_answer`, `phone_hangup`) — each anchored on a trigger word unclaimed
elsewhere in the table (`weather`, `youtube`, `call`, `hang up`), so they're safe ahead of
the generic desktop open/close rules without needing that pair's "most specific wins" care.
`weather`/`youtube_play` capture the **whole** utterance rather than a sub-phrase, because
`weather.py`'s override parsing and `youtube.py`'s filler-stripping already parse the full
text themselves — capturing less here would just be a second, redundant normalizer.

**What's still missing.** `docs/ARCHITECTURE.md` had no Phase 35 section for the goal
orchestrator (`core/orchestrator.py`, added in the commit immediately before this phase
began) when this was written — that gap predates Phase 36 and wasn't backfilled here, since
documenting another author's design decisions secondhand risks getting the "why" wrong in a
way that's worse than leaving it openly missing.

---

### Phase 37 — World news awareness (`skills/world_news.py`)
Calvin: *"an agent that is aware of the state of the world in all fronts... even when making
decisions we are aware of the surrounding situation"*. A daily "what's up today" briefing —
world & conflicts, AI/tech, sports, business, Kenya — reachable on demand (`/news`,
`/whatsup`, voice, dashboard chat) or pushed automatically every morning. Nine free RSS wires
(no API key, §0 free-first), same never-invent-a-source discipline `research.py` already
enforces: every line traces to an actual fetched headline, and an empty category says so
rather than being padded out. Explicitly **not** market prediction or trading advice — see
Phase 38 below for real price data correlated with news, never a forecast.

Built in five ordered sub-phases (37b), each depending on the last:

- **S1 — dedup by embedding.** Headlines are clustered into stories by cosine similarity over
  the same NIM-hosted embedder `core/semantic.py` uses for persona/CV recall (`core.embeddings.
  get_embedder("auto")` + `cosine()`), not title-string matching — it catches the same event
  worded differently and, just as importantly, does not merge two different stories that
  happen to share a word. Clustering is in-memory and per-run only; nothing here writes to
  `semantic_index`, which is a closed namespace (fact/note/doc) for durable recall, not a
  day's transient headlines. An embedder failure degrades to one story per headline rather
  than going silent. **Bonus find while verifying this against real data:** `NimEmbedder`'s
  original model, `baai/bge-m3`, returned a bare 500 on every call to this account's NIM
  endpoint — meaning semantic recall (including the documented "~71% context reduction" on
  CV tailoring) had likely been silently running on the hashing fallback since Phase 33, with
  nothing anywhere surfacing it as broken. Switched to `nvidia/nv-embedqa-e5-v5` (same
  1024-dim, no schema change).
- **S2 — "since last asked" watermark.** `world_news_delivered` (category, url, delivered_at,
  retired_at) tracks which headline URLs have already been shown; asking again mid-morning
  surfaces only genuinely new stories instead of replaying the 24h window. The watermark
  itself is `MAX(delivered_at)` derived from this same table rather than tracked separately,
  so the scheduled push and the on-demand path share one source of truth and can never drift.
  `full=True` (or the word "full" in the request text) forces the whole window regardless.
- **S3 — source diversification + independence weighting.** Every `Source` (a `NamedTuple`:
  name, url, group) carries an independence GROUP — shared ownership/editorial control, not
  just a shared feed. All BBC feeds share one group; Nation Africa and Business Daily Africa
  share one too (both Nation Media Group) — without that tag, one publisher's two outlets
  covering a story would inflate corroboration to 2 when there's really one editorial voice
  behind it. `Cluster.corroboration` counts distinct groups, never raw headline count.
  Reuters and AP were tried first (per the original ask) and dropped after verification —
  neither has a working free RSS feed left; The Guardian and UN News substitute for
  editorially-independent world coverage, and arXiv cs.AI / OpenAI's blog give AI/tech a
  dedicated releases source instead of leaning entirely on TechCrunch's general firehose.
- **S4 — cross-category front page.** The brief leads with "TOP STORIES" — the 3 most
  important stories across ALL requested categories, picked by a fully deterministic score
  (magnitude × corroboration × configured interest relevance — `world_news.interest_order`).
  The model only writes the prose for whatever the score already chose; ranking never varies
  run-to-run for identical clusters. Only shown for multi-category requests — a single
  category doesn't need a front page on top of itself.
- **S5 — corroboration-gated breaking news.** A separate, more frequent poll
  (`check_breaking`, `world_news.breaking_poll_minutes`) fires **at most one** push, and only
  when a story clears every bar: corroboration ≥ `breaking_corroboration_bar` (default 3
  independent groups), coverage published within `breaking_window_hours` of each other
  (default 6h — tightly time-clustered, not just eventually corroborated), never already
  pushed for this story-lineage, and a **global** cooldown (`breaking_cooldown_minutes`,
  system-wide, not per-category) has elapsed. There is no keyword path anywhere — a
  single-source headline titled "BREAKING URGENT WAR ALERT" is handled identically to a
  mundane one. Deliberately conservative: this is a rare, high-bar exception to pull-only
  delivery, not a second firehose — the exact failure mode Phase 34's own queue-drowning
  incident (83 drafted jobs nobody could review) already taught this codebase to design
  against. On a DB outage the cooldown check fails **closed** (assumes "just fired") rather
  than risking a flood on uncertainty.
- **S6 — config-driven sources.** Categories and sources moved from a hardcoded Python dict
  to `config.yaml`'s `world_news.sources` (`_feeds()` reads it live, falling back to
  `DEFAULT_FEEDS` if that section is missing or malformed) — adding, removing, or re-grouping
  a source is now a config edit, not a code change.

`respect_robots=False` at the RSS fetch site is scoped deliberately narrowly: valid for a
feed published specifically to be polled, the same reasoning `research.py`'s
`DuckDuckGoSearcher` already uses for a search endpoint. It does not extend to fetching an
article's full body — a future "read more" would need to consult robots there.

### Phase 38 — Markets snapshot (`skills/markets.py`)
The deferred half of Calvin's original "world awareness" ask ("...train it on all market
moves... predict markets... probability of dumping on gold/oil due to wars"). Pushed back
on explicitly before building anything: no model reliably predicts markets, and presenting
a confident forecast would itself be a fabrication (§0 P5). Scope confirmed via
`AskUserQuestion` — **"data + news correlation only"**: `/markets` (and an automatic daily
push, `markets.briefing_hour`/`briefing_minute`) shows real prices and % moves for crypto,
forex, gold/oil, and major stock indices, plus a news-correlation line for any notable
mover — never a prediction, forecast, or buy/sell/hold call, under any framing. Every
rendered snapshot ends with an explicit "Data only — not financial advice" line; the
constraint is visible in the product, not only enforced upstream of it.

Data sources (free, keyless, each verified live via `curl` before being wired in):
- **CoinGecko `/simple/price`** — one batched call for every configured crypto id
  (Bitcoin/Ethereum/Solana by default), 24h % change included in the response.
- **Yahoo Finance's `/v8/finance/chart/{symbol}`** — forex pairs (`KES=X`, `EURUSD=X`,
  `GBPUSD=X`), commodities (`GC=F` gold, `CL=F` crude oil/WTI), and stock indices
  (`^GSPC`, `^IXIC`). One request per symbol: Yahoo's batched `/v7/finance/quote` endpoint
  now returns `Unauthorized` without a session cookie/crumb the chart endpoint doesn't
  need. % change isn't in the chart response, so it's computed from
  `regularMarketPrice` vs `previousClose`/`chartPreviousClose`.
- **Stooq was tried first and dropped** — its public CSV quote endpoint (`stooq.com/q/l/`)
  currently returns a "page has moved" HTML response across every URL/domain/User-Agent
  variant tried, not CSV data.

`respect_robots=False` here is a materially different call from Phase 37's RSS exception,
and was surfaced and confirmed explicitly rather than assumed by precedent: CoinGecko's
`robots.txt` explicitly disallows `/api/v3` (the exact endpoint used), and Yahoo's
finance-quote host disallows everything — both explicit path-level `Disallow` entries, not
merely absent ones. Confirmed as an intentional exception: both are documented/de-facto
public JSON quote APIs meant to be polled by tools (CoinGecko publishes this exact
endpoint as its API; Yahoo's chart endpoint is the one every finance library uses), not
article/content pages — scoped only to these two price endpoints, never to an
article-body or HTML page fetch.

News correlation reuses `WorldNewsSkill.recent_headlines()` (business + world categories)
rather than duplicating RSS-parsing logic — one set of feeds, one place that owns "what's
in the news right now." Only quotes moving at least `markets.notable_move_pct` (default
2%) trigger the single LLM call that writes the correlation commentary, and only when at
least one headline was actually fetched — a flat market or an empty news pull renders the
raw numbers with no commentary section, never an invented one. The prompt mirrors
`world_news._synthesize`'s discipline (cite only headlines actually fetched, hedge
causation language) plus its own hard line: no prediction in any timeframe, no buy/sell/
hold advice under any framing, even hedged. Instruments are config-driven from the start
(`config.yaml`'s `markets.instruments`, falling back to `DEFAULT_INSTRUMENTS`) — Phase 37
only reached that pattern in a later slice (S6); no reason to relearn the lesson here.

### Phase 38b — The self-driving stage (`core/stage.py`, `core/presenter.py`, `skills/stage.py`, `frontend/src/ui/stage/`)
Extends Phase 36's React dashboard with a choreographed visual layer that shows *what* is
being talked about — a live chart, corroborating headlines, a fact — around the reactor
ring, without adding a second source of truth for data or a second authority for actions.
Visual/interaction spec: `agentos_stage_prototype.html` (a static hand-drawn-canvas
prototype, demo data, timer-driven auto-cycle — none of that is ported; see below). The
prototype's own "AGENTOS_BUG_LIST.md #17" reference does not exist as a file anywhere in
this repo or its parent directory; the lesson it names (a corroboration-gated, never-
keyword-gated breaking-news bar, S5 above) is independently and fully documented in
`world_news.py` itself, so this was treated as a missing citation, not a blocking gap.

**The primitive.** A `StageDirective` (`core/stage.py`, mirrored exactly by
`frontend/src/core/stageTypes.ts`) carries `focus`, an optional `headline`, an `accent`
(`primary` | `alert`), a `transition` (`bloom` | `swap` | `settle`), an optional `ttl_s`,
and a list of typed `widgets` (`chart` | `articles` | `ticker` | `fact` | `map`). It is
display data only — no widget type can carry an action, and building one never touches
`core/approvals.py`. `kernel/app.py`'s `/ws/voice` reply carries it alongside the existing
`{ok, text, intent, skill, ...}` shape as an optional `directive` key, present only when
there is something to say about the stage; its absence (not `null`) means "leave whatever
is currently shown alone," distinct from an explicit idle directive (`focus: null,
widgets: []`), which means "go back to calm."

**`core/presenter.py` is a router, not a data source.** `build_directive(intent, result)`
maps a turn to a directive by asking `skills/markets.py`'s and `skills/world_news.py`'s own
new `display()` methods for widgets built from what those skills already fetch for their
ordinary commands (`snapshot`/`whats_up`) — no new fetch, no new LLM call (both `display()`
methods are synthesis-free and fast enough to run on every subject change). Three routes:
`stage.*` actions (voice-steered, below), `markets.{snapshot,display}`, and
`world_news.{whats_up,display}`; anything else returns `None`, and a builder exception
degrades the same way — the reply still lands, the stage just doesn't change.

**Voice steering (`skills/stage.py`), not DOM-poking.** "Show oil", "pin that", "back to
idle" are ordinary routed commands (new keyword rules in `core/intent.py`: `stage_focus`,
`stage_pin`, `stage_unpin`, `stage_idle` — `show`/`pull up` sit at the very end of the
keyword table, after every rule with a real prior claim on those words, e.g.
`email_search`'s "show my emails from..."). `focus(subject)` decides whether the subject is
a market asset (`skills/markets._resolve_asset`, a small alias table over the configured
instrument list — "oil" → `Crude Oil (WTI)`) or a news topic
(`WorldNewsSkill._resolve_topic`, same pattern over configured categories), and hands that
decision to the presenter via `CommandResult.data` — it never builds a widget itself.
Pin/unpin carry no data at all; they're recognized by intent NAME alone
(`msg.intent === "stage_pin"`) on the frontend, since pinning is pure client-side state
(which widget is frozen), not something the server needs to know.

**The one catalyst.** `world_news.check_breaking()` (S5, the corroboration-gated
breaking-news poll) is the sole event allowed to re-choreograph the stage without Calvin
having said anything. It pushes a `StageDirective` (accent `alert`, a `FactWidget` with the
real corroboration count, an `ArticlesWidget`) through `core/stage.py`'s `StageBus` — an
in-process registry of connected `/ws/voice` sockets. `check_breaking` is an *unqueued*
APScheduler job (runs in the API process, not the worker), but `AsyncIOScheduler` executes
plain sync callables on a worker **thread**, not the event loop — so `StageBus.push()` is
the thread-safe entry point, handing the broadcast coroutine to the loop captured once at
kernel startup (`lifespan()`) via `run_coroutine_threadsafe`. A push with no loop bound or
no sockets connected is a silent no-op; a catalyst must never raise because nobody's
watching.

**Chart data: extended fetches, not a new data source.** `markets.py` already fetched
CoinGecko's `/simple/price` and Yahoo's `/v8/finance/chart/{symbol}`, but only read a
single current price. `display()` adds `_fetch_crypto_chart_series` (CoinGecko's own
`/market_chart` endpoint — same host, same robots.txt exception already justified in that
module's docstring) and `_fetch_yahoo_chart_series` (the exact same Yahoo chart endpoint,
now asking for the `range`/`interval` series it always returns instead of only `.meta`).
Every `ChartWidget` carries `as_of` (the last real tick's timestamp) and a `delayed_label`
keyed by source — `"real-time"` for CoinGecko, `"~15m delayed (free feed)"` for Yahoo — on
the widget itself, not just upstream of it. No series → the chart widget is omitted, never
synthesized or interpolated; a null point from either API is dropped, not smoothed over.

**News images: feed thumbnails, or the typed fallback — never a search.**
`world_news._extract_image` reads `media:thumbnail` / an image `media:content` / an image
`enclosure` directly off the RSS `<item>` (namespace-agnostic tag matching) — the *only*
place `Headline.image` is ever set anywhere in this codebase. `display(topic)` reuses the
existing fetch+cluster pipeline (no LLM call) and resolves `topic` via a small alias table
over the configured categories (`_resolve_topic`), returning `None` — omitted, never a
placeholder — for an unresolvable topic or nothing fetchable. Frontend:
`ui/stage/ArticlesWidget.tsx`'s `<Thumb>` renders the feed image if present, and falls back
to the same typed card (a plain icon, not a broken `<img>`) on a missing image **or** a
runtime load failure (`onError`) — there is no third path that substitutes a searched or
generic photo.

**Chart rendering: hand-rolled canvas, not a new dependency.** Rather than adding
uPlot/lightweight-charts, `ui/stage/ChartWidget.tsx` draws the (fixed, historical) series
on a plain `<canvas>` — one draw per dataset, no rAF loop — the same architecture
`ReactorRing`/`Waveform` already use, and the same technique the prototype itself uses
(hand-drawn canvas, no library). This keeps the bundle dependency-free, keeps the widget
entirely out of the animation budget below (nothing to animate continuously), and avoids
pulling a third-party canvas-heavy library into a jsdom test environment that has no native
`canvas` package (`frontend/src/test/setup.ts`'s shared fake 2D context stub was extended
with `closePath`/`createLinearGradient` for this).

**Choreography (`ui/stage/StageCanvas.tsx`, the Director).** Widgets are assigned to fixed
slots by type (ticker → top strip, chart → right, fact/map → left, articles → bottom
strip), matching the prototype's layout. Transitions are enter-only (`bloom`: fade + rise;
`swap`: crossfade; `settle`: instant) — mirroring the prototype's own choreography exactly
(`clearSlots()` hides the old scene instantly, then rebuilds and blooms the new one; there
is no fade-out racing a fade-in to coordinate, which also makes a swap a synchronous fact
rather than something gated behind an animation's completion callback). `useReducedMotion()`
collapses every transition to instant and starts no loop. The shared
`ui/stage/animationBudget.ts` caps concurrently-animating widgets at 2 (the ring is
separate and always allowed) — a widget denied a slot renders statically rather than
queuing; `ui/stage/useOffscreenPause.ts` (IntersectionObserver, fails open) frees a slot
the moment its widget scrolls out of view. **Event-driven only**: there is no
`setInterval`/timer anywhere in `StageCanvas.tsx` (asserted directly by
`test_stage_canvas...source contains no interval-driven scene rotation`) — the prototype's
own `setInterval` auto-cycle (a demo aid, explicitly not production behavior per its own
on-page note) is not ported. The one client-side timer is `ttl_s`'s idle-return
(`setTimeout`, re-armed only when the directive *reference* changes, i.e. once per real WS
event) — a purely local decay back to calm, needing no server round-trip.

**Pin.** `pinCurrentWidget()`/`pinWidget()`/`unpinWidget()` live in `core/store.ts`; a
pinned widget is merged back on top of every subsequent directive
(`StageCanvas.mergeWithPin`) for its widget type, surviving even a directive that doesn't
mention that type at all, until explicitly unpinned — voice ("pin that"/"unpin", matched by
intent name) and a manual per-widget button both end at the same store actions. Priority
for "pin that" with no explicit target: chart, then fact, then articles.

**Degrades to Phase 36.** `StageCanvas` renders the *exact* pre-Phase-38 idle markup
(ring + waveform + mic button) whenever there's no directive and nothing pinned — a
presenter failure, an empty `display()`, or simply "nothing asked yet" are visually
indistinguishable from before this phase ever existed.

### Phase 39 — Research reports: always a polished PDF (`skills/research/`)
Upgrades the research skill to fix real, observed production bugs, all from one transcript:
"make a 3-page doc" answered inline instead of producing a file; `/research` had no route
at all (only `/find` worked); the format directive ("3page doc") was searched literally;
citations were mangled (`Chevrolet_Camaro￼[3]`, a date captured as a ref); the reply sent
twice; and — the one that made this an accuracy bug, not a polish one — essay-mill sourcing
(Scribd, Bartleby) produced a real factual error: the output claimed the Camaro "is one of
the last remaining muscle cars still in production today," when GM actually retired the
sixth generation at the end of the 2024 model year. `Camaro_Research_Report.pdf` /
`_v2.pdf` (the visual target named in the build brief) do not exist anywhere on this
machine or its parent directory — confirmed by an exhaustive search — so this was built
from the brief's own exhaustive textual furniture spec (palette hexes, cover elements,
running header/footer, reference/figure/table style) rather than a pixel-matched render;
same for `AGENTOS_BUG_LIST.md #15` (double-send), whose lesson is independently documented
below and in `skills/job_hunter/skill.py`'s own comment on the identical bug class.

**PDF is a routing rule, not a per-request choice.** Every path into this skill — the
`research`/`find` commands (`skills/telegram_bot.py`'s `COMMAND_MAP`; `/research` had NO
entry before this phase — that was the actual routing bug), and the keyword rules in
`core/intent.py` for "search/research/look up/look into X", "make/write/create/build a
doc/report/pdf/paper (about) X", and "X, N-page doc" — all resolve to the same
`ResearchSkill.search()`. There is no inline-text branch and no "short answer" fast path;
`search()` always builds and delivers a document. A DIFFERENT, narrower method,
`research()`, still returns a quick cited-text `ResearchResult` for OTHER skills'
own grounding (`interview_prep.py`'s company research, `study_vault.py`'s web fallback) —
those are inputs to a different skill's output, never "the research report" shown to
Calvin, so they keep the old fast, snippet-based path deliberately.

**Package layout** (`skills/research/`, mirroring `job_hunter/`'s modular split):
- `request_parsing.py` — `parse_request(text)` splits TOPIC from format/length/extras
  ("3-page"/"brief"/"detailed", "with a cover letter to X") in exactly one place, reusing
  `core.intent`'s own punctuation normalizer rather than re-solving Whisper's curly-quote
  inconsistency a second time. Prefix framing ("make a doc about X") and suffix framing
  ("X, 3-page doc") are both stripped, the latter anchored to the END of the string only —
  an unanchored strip would just as happily eat "report" out of "the Mueller report".
- `gather.py` — `search_authoritative()` filters every essay-mill/content-farm domain
  (`ESSAY_MILL_DOMAINS`: Scribd, Bartleby, Coursehero, and similar; `research.
  blocklist_domains` in config.yaml only ever EXTENDS this, never shrinks it) before a
  result is even fetched; `gather_sources()` then fetches the FULL page (never hands a
  bare search snippet to synthesis — a snippet is the search engine's relevance guess, not
  evidence a fact is actually stated on the page), degrading to the snippet only if the
  fetch itself fails.
- `synthesis.py` — one `llm.chat_json("research", ...)` call turns the fetched sources into
  a structured `DocumentPlan` (title/subtitle/abstract/sections/table), instructed to cite
  every claim `[n]` against only the given source numbers, to hedge or OMIT a load-bearing
  detail (date/status/figure) supported by just one source, and to paraphrase — never copy
  a source's sentences verbatim (short attributed quotes excepted). `LLMError`, or a
  response with no usable sections, degrades to `_degrade_to_fact_list()` — the sourced
  snippets themselves, still individually cited, never a fabricated narrative and never
  silent.
- `images.py` — `WikimediaImages.find_image()` searches Commons's own free API, and embeds
  a candidate ONLY after its own `extmetadata` verifies an open license (CC/public domain
  markers checked against Commons's actual `LicenseShortName`/`LicenseUrl`, never guessed
  from a filename). No open-licensed candidate anywhere in the result set → `None`, and the
  caller renders the typed placeholder box — never an unverified image, never a search
  substitute for a missing one.
- `house_style.py` — the ONE place the look lives: palette (dark green + brass — `ink`,
  `soft`, `accent`, `hairline`, `fig_fill`), the byline (`research.byline` in config.yaml,
  default `"Research by Momanyi Kelvin"`) and monogram initials (`research.
  monogram_initials`, default `"KM"`) are all config reads, never literals, so two
  different-topic reports render identically and retuning the look is a one-line config
  edit. Furniture helpers: the cover (kicker, title, accent rule, byline, source count,
  date), the running header (topic left / "RESEARCH REPORT" right + rule — every page
  EXCEPT the cover, whose own title already states the topic in full) and footer (the
  byline as a signature line + page number, on every page including the cover), the brass
  monogram ring (top-left of the cover only), figure/placeholder/table/reference flowables,
  and an optional one-page cover letter (off by default; only built when a "with a cover
  letter" directive is parsed).
- `pdf_builder.py` — assembles a `DocumentPlan` + sources + an optional image into the
  final ReportLab `SimpleDocTemplate`, calling `house_style.py` for every piece of look and
  owning only layout/ordering: cover → contents (auto-included once section count reaches
  5, a deterministic proxy for "roughly 5+ pages" that doesn't need a two-pass render) →
  abstract → numbered sections (one figure or its placeholder placed after the first
  section's own text) → table (only if the plan has one) → references.
- `skill.py` — `ResearchSkill.search()` orchestrates parse → gather → synthesize → image →
  build → **deliver once**. The double-send fix mirrors `job_hunter.approve()`'s own
  documented fix for the identical bug class: the document goes out through
  `core.notify.send_telegram_document()` — called exactly ONCE — and the `CommandResult.
  text` returned to the caller's own reply channel is a short confirmation, never the
  report's content, so the two literally cannot carry the same thing twice. There is no
  "dashboard download" delivery path anywhere in this codebase (checked before reusing it,
  per the build brief's own instruction) — Telegram document push, already proven by
  `job_hunter.py`, is the one real mechanism, reused as-is.

**Copyright discipline.** Synthesis is instructed to paraphrase, never reproduce — the
report is original prose grounded in fetched sources, not a stitched-together copy; short,
quoted, attributed fragments are the only verbatim exception.

---

## 9. Conversational state machines

Three flows keep session state in `kv` (JSON) so they work identically over voice, Telegram
voice notes, the dashboard, and CLI, and survive restarts:

- **Mock interview** (`interview_prep.mock` / `mock_answer`) — one question at a time.
- **Quiz** (`spaced_rep.session`) — reveal → grade, or judged voice answers.
- **Tutor** (`code_tutor.session`) — drill solutions / socratic answers / lab submits.

In the Telegram bot, `route_text()` checks for an active session and routes free text to the
right continuation before falling back to the general intent router.

**Sessions are one-shot (Phase 34).** A continuation is consumed once and the session ends
immediately; the next message routes fresh. A sticky session is a *mode*, and a mode you forgot
you were in silently rewrites the meaning of everything you say next — a `/tutor` session ran
for two days and turned an email request into an `smtplib` tutorial and a playlist request into
C++ classes. A TTL shortened that window without changing the shape of the failure. The session
is cleared **before** dispatch, not after, so a skill that raises still leaves the mode gone.
Re-entry is by keyword (`tutor`, `quiz me`, `mock interview`, `create a playlist`), which is why
those route deterministically in `core/intent.py` rather than via the LLM.

`email_agent`'s send/trash previews are the deliberate exception: those are a two-step
**confirmation**, and one that forgets what it is confirming would be worse than useless.

Because all of this is **server-side and keyed to Calvin rather than a device**, these flows
are what Phase 19's hand-off resumes: `live_skill_session()` reads those same `kv` keys, so
"where were we?" from the phone finds the mock interview started by voice. The flip pipeline
(`pipeline_state`) is a different animal — a durable, database-enforced state machine with an
immutable transition log, not a conversation.

---

## 10. Testing philosophy

- **859 tests** (5 fail only in a local Postgres cluster missing the `pgvector` extension —
  environmental, not a code failure; see `test_semantic.py`). Every external service (NIM, Gmail, HTTP scrapers, Telegram, Spotify, sockets,
  TLS, OSV) and the clock are injected or mocked — `pytest` needs no API keys and hits no
  network. The one real dependency is **PostgreSQL**: `TEST_DATABASE_URL` points at a test
  database, and each test runs in its own schema (created once per session, truncated between
  tests).
- ⚠️ `tests/conftest.py` reads `TEST_DATABASE_URL` *after* importing `core.llm`, which loads
  `.env` into the environment — so **`.env` silently overrides the conftest default**. If the
  suite seems to stall on the first database test, that DSN is pointing at a host that isn't
  listening. `Memory` sets `connect_timeout=10` so this now fails with the (redacted) DSN
  rather than hanging.
- `pytest.ini` already sets `-q`. Passing `-q` again makes `-qq`, which **suppresses the final
  "N passed" line** — the run is fine, the report just disappears.
- The destructive truncate lives in `tests/conftest.py`, deliberately **not** on `Memory` —
  the production data layer must never expose a bulk-delete (§0 Principle 4).
- Pure logic (SM-2, ATS, embeddings, intent rules, timetable, deal scoring, callback parsing) is
  factored out and unit-tested directly.
- Skills take injectable dependencies (memory, llm, fetcher, searcher, transcriber, mailer,
  port/TLS/HTTP probes, OSV, Spotify, clock) so their behaviour is tested without side effects.
  Write clocks are **caller-governed** (`posted_at=`, `now=`) rather than `time.time()` inside
  `Memory`, so an injected clock actually reaches the database.
- **Guardrail tests** assert the §0 rules hold: nothing sends pre-approval, the persona never
  fabricates, assessments aren't auto-solved, no voice/face-cloning code path exists anywhere,
  the deal broker exposes no way to spend money or message a stranger, and the recon skill
  exposes no mutating verb. Later additions: expiry **never deletes a row** and never touches
  work Calvin has acted on; `high`-tier actions can never be learned into auto-approval; job
  expiry's deadline parser refuses to invent a deadline it cannot cue on; and playlist removal
  can never be reached by an email-deletion phrase (that misroute was real — the catch-all
  trash rule matches the verb "remove", so "remove X from my playlist" reached `email_agent`).
- **Nothing in the suite may push to real Telegram.** An autouse fixture severs the transport
  for the whole session. This is not hypothetical: three skills whose `notify` defaults to
  `True` fired live messages at Calvin's phone on every full-suite run — an interview invite, a
  lecture he never recorded, a deadline that did not exist — for a whole night before he showed
  us the chat log. Patching the call sites would have fixed that day and rotted the next, so the
  sender itself is blocked and a test that forgets now fails loudly instead of texting a human.

Run: `pytest` (or `pytest tests/test_<area>.py`).

---

## 11. Adding a new capability

1. Create `skills/my_skill.py` implementing the interface and exposing `SKILL`:
   ```python
   from core.skill import BaseSkill, CommandResult, ScheduledJob, SkillContract
   class MySkill(BaseSkill):
       name = "my_skill"
       def contract(self):        return SkillContract(reads_categories=["study"])
       def commands(self):        return {"do": self.do}
       def scheduled_jobs(self):  return [ScheduledJob("my_skill.tick", self.tick, "interval", {"hours": 1})]
       def do(self, **kw):        return CommandResult(text="done")
   SKILL = MySkill()
   ```
2. **Declare a contract** (Phase 20). Omit it and your skill reads **no** standing instructions —
   that's the safe default, not an oversight. Name only the categories you genuinely act on, and
   add any skill-specific `hard_invariants`; the universal ones are added for you.
3. (Optional) add an intent rule in `core/intent.py` and a Telegram command in `telegram_bot.py`.
4. Use `llm.chat("<task_class>", …)` — pick the task class whose model fits the work.
5. Take your side effects as **injectable dependencies** (memory, llm, fetcher, clock, …) — this
   is what keeps the suite offline.
6. Add tests with injected deps. That's it — the kernel discovers it at boot and registers its
   contract; no kernel edits.
