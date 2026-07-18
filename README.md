# AgentOS

A 24/7 personal agentic system for **Calvin** — full-stack developer & CS student
(Meru University of Science & Technology, Year 3), focused on growing his **cloud
computing & DevOps** skills. AgentOS hunts jobs, manages email, answers as Calvin,
prepares him for interviews, runs a side-hustle deal pipeline, audits his own
infrastructure, and doubles as a full study companion — reachable by **voice** (laptop),
**Telegram** (phone), **dashboard** (browser), and **CLI**.

> **Status: all 23 phases complete.** 21 skills · 23 scheduled jobs · **471 tests passing**
> (all offline, network mocked). See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the
> full explanation of every capability and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) to run it.

---

## What it does

| Area | Capability |
|------|------------|
| **Job hunting** | Scrapes 9+ sources → category-scores → drafts a cover email from your verified facts → digest with Apply/Skip/Tailor buttons → sends on approval with a tailored CV → tracks applications → watches for interview invites |
| **Email** | Hourly inbox cleanup (classify + archive/label) · explicit recoverable Trash with preview, confirmation, and undo · reply drafting (never sends) |
| **Persona** | Answers *as you* from verified facts only (never invents) · standing instructions · learns your style from your edits |
| **Forms & interviews** | Fills application forms / screeners from your KB (flags unknowns) · interview prep packs (PDF) · `/mock` rehearsals |
| **Voice** | Wake-word laptop client (local STT) · pre-built neural voices only · barge-in · push-to-talk |
| **Telegram** | Full remote control · inline approval buttons · voice notes transcribed & routed |
| **Study** | RAG over your course notes (cites file+page) · lecture audio → notes+flashcards → PDF · SM-2 spaced repetition · code tutor (explain/review/drill/socratic/mock-lab) |
| **Planning** | Timezone-aware daily briefing · week planner · exam cram mode (mock CAT PDF) · deadline tracking with overdue warnings |
| **Events** | Free events matching your interests (CTF, DevOps, hackathons…) → planner |
| **CV** | ATS-optimized variant per job (reorders/emphasizes what's true, never fabricates) · keyword score before/after |
| **Deal broker** | Sources underpriced local listings → scores → drafts your negotiation → cross-posts a resale → first-committed-buyer-wins → margin ledger. **Never spends money, never messages a stranger as you** |
| **Adaptive layer** | Notices repeated patterns and *proposes* a rule for you to confirm — never self-modifies. Skill Contracts bound which rules can reach which skill |
| **Continuity** | One session across phone/laptop/browser/CLI — hand off mid-task, see every pending approval in one place |
| **Self-audit** | Weekly report-only scan of infrastructure *you enrol*: open ports, TLS expiry, exposed config, CVEs via OSV, container health. **Never acts** |
| **Music** | Spotify taste model, sequencing, playlists, transport control, narrated DJ mode (stock voice only) |
| **Desktop** | "open Spotify", "close VS Code" on your laptop by voice. **Allowlisted apps only** (your laptop decides, not the server) and **graceful close only** — no force-kill, so unsaved work is never lost |

## Non-negotiable principles ([§0](docs/ARCHITECTURE.md#0-non-negotiable-principles))

- **Free-first LLMs** — every call goes through NVIDIA NIM; no paid APIs, no OpenAI.
- **Best model per task, routed not hardcoded** — a coder model reviews code, a reasoning
  model does research, a cheap model classifies. Each task class can even use its **own API
  key** so concurrent work doesn't throttle itself (see [Model routing](#model-routing)).
- **Approval gates** on anything sent in your name. **Never permanently deletes data** (email Trash is recoverable and requires an exact preview/confirmation). **Never fabricates
  facts about you.** **No face or voice cloning — ever.**
- **Everything is a Skill** — self-contained, auto-discovered; adding one never touches the kernel.
- **No undisclosed personas** — every message a stranger receives is sent by you (drafted by the
  bot), never by a bot posing as you.

## Architecture at a glance

```
Laptop  ── voice_client.py ──WSS──┐
Phone   ── Telegram / shortcut ───┤
Browser ── /dashboard ────────────┤
CLI     ── manage.py ─────────────┤
                                  ▼
        ┌──────── DigitalOcean droplet ────────┐
        │  kernel  (FastAPI + APScheduler +     │   two PM2 processes:
        │           skill registry + router)    │   • agentos-api  (kernel + scheduler)
        │  skills/ (21 auto-discovered skills)  │   • agentos-bot  (Telegram)
        │  core/   (llm router, memory/Postgres,│
        │           intent, persona, embeddings…)│
        └───────────────────────────────────────┘
```

All four channels share **one server-side session keyed to Calvin, not to a device** — so a
mock interview started by voice continues on the phone.

## Quick start — Docker (recommended)

Brings up Postgres + the kernel + the Telegram bot. Nothing to install but Docker.

```bash
cp .env.example .env          # NVIDIA_API_KEY + AGENT_WS_TOKEN at minimum
docker compose up -d --build
curl -s localhost:8000/api/health | python -m json.tool

docker compose --profile test run --rm tests      # the full suite, isolated database
docker compose exec api python manage.py health   # any CLI command
docker compose logs -f bot
```

`api` and `bot` are the same image run with different commands — restarting one never touches
the other. Ports bind to `127.0.0.1`; use the `tls` profile to put Caddy in front for public
access. Full detail: [docs/DEPLOYMENT.md § 0](docs/DEPLOYMENT.md#0-docker-the-recommended-path).

## Quick start — without Docker

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/mac: source .venv/bin/activate
pip install -r requirements.txt

# PostgreSQL is required (raw SQL, no ORM). Create the dev + test databases:
createdb agentos && createdb agentos_test

cp .env.example .env          # set DATABASE_URL + NVIDIA_API_KEY (+ optional per-task keys)
pytest                        # 471 tests (external services mocked; needs the test DB)

python manage.py health                    # subsystem snapshot
python manage.py serve                     # start the kernel on :8000
python manage.py command "any new jobs?"   # route one command (keyword-only, offline)
```

Seed yourself, then use it:
```bash
python manage.py persona-init              # interactive interview → verified facts
# drop your CV at data/cv/master_cv.pdf, then:
python manage.py cv-update
# drop notes in data/vault/<UNIT>/, then:
python manage.py ingest
python manage.py briefing --no-send        # your morning briefing
python manage.py quiz                      # spaced-repetition review
python manage.py tutor "drill linked lists"
python manage.py infra enroll <host> --ports 80,443   # then: infra scan  (report-only)
```

> `TEST_DATABASE_URL` is read from `.env` if present — it overrides the test default. If the
> suite appears to stall on the first database test, that DSN is pointing somewhere nothing
> is listening.

## Model routing

Every LLM call declares a **task class**; `config.yaml → llm.routes` maps it to a model,
and optionally a **per-task API key**, endpoint, and generation params:

```yaml
routes:
  classify:    { model: "meta/llama-3.1-8b-instruct",   api_key_env: NVIDIA_API_KEY_FAST, temperature: 0.0 }
  write:       { model: "mistralai/mistral-medium-3.5-128b", api_key_env: NVIDIA_API_KEY_WRITE }
  code_review: { model: "deepseek-ai/deepseek-v4-pro", api_key_env: NVIDIA_API_KEY_CODE }
  research:    { model: "nvidia/nemotron-3-super-120b-a12b", api_key_env: NVIDIA_API_KEY_RESEARCH, max_tokens: 1500 }
```

- **Best model per task** — coding → coder model, research → reasoning model, high-volume
  classification → cheap fast model.
- **Separate keys for concurrency** — because the scheduler, Telegram bot, and voice can all
  call the LLM at the same time, giving each task class its own key spreads them across
  separate rate-limit buckets. Any unset key **falls back to `NVIDIA_API_KEY`**, so one key
  still works everywhere. Full detail: [ARCHITECTURE → Model routing & concurrency](docs/ARCHITECTURE.md#model-routing--concurrency).

## Interfaces

- **CLI** — `python manage.py <cmd>` (see `manage.py --help`): serve, health, auth, cleanup,
  digest, draft, hunt, approve, summary, watch, persona-init, form, research, prep, mock,
  voice, telegram, ingest, ask, vault, lecture, quiz, cards, review-report, tutor, review,
  briefing, plan, cram, deadline, events, cv-update, cv, **flip**, **infra**, **music**,
  **adaptive**, backup.
- **Telegram** — `/status /jobs /approve /ask /find /form /prep /mock /draft /facts /quiz
  /cards /tutor /briefing /plan /cram /deadline /events /tags /cv /rules /retro /contracts …`,
  inline buttons, and voice notes. Free text → the same intent router as voice.
- **Voice** — say the wake word on your laptop; see [`client/README.md`](client/README.md).
- **Dashboard** — `/dashboard` in a browser (same Bearer token); the fourth channel on the
  same session.

## Docs

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — how everything works, phase by phase; the
  data model; model routing & concurrency; the design principles and how each is enforced.
- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — droplet setup, API keys, Gmail OAuth, PM2,
  the laptop voice client, phone access, and nightly backups.
- **[client/README.md](client/README.md)** — laptop voice client setup.

## Tech stack

Python 3.11+ · FastAPI + APScheduler · **PostgreSQL** (raw SQL via psycopg 3 — no ORM) · WebSocket ·
python-telegram-bot · google-api-python-client (Gmail) · feedparser + BeautifulSoup
(scraping) · ReportLab (PDFs) · sentence-transformers *(optional)* / hashing embedder
(vault) · faster-whisper + edge-tts *(voice, laptop-side)* · Africa's Talking (SMS/WhatsApp) ·
Spotify Web API *(optional, Premium)* · OSV.dev (CVE lookups, keyless) · NVIDIA NIM (all LLMs) ·
PM2 + Caddy (deploy).
