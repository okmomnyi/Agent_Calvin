# AgentOS — Deployment Guide

How to run AgentOS in production: a DigitalOcean Ubuntu droplet (API + Telegram bot), the
laptop voice client, phone access, and backups. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for
how it all works.

**Two supported ways to run it**, and they deploy the same code:

| | [**Docker**](#0-docker-the-recommended-path) (recommended) | [**Bare droplet + PM2**](#4-droplet-setup) |
|---|---|---|
| Setup | `docker compose up -d --build` | venv + Postgres + PM2 + Caddy, by hand (§3b–§8) |
| Postgres | in the stack, on a volume | you install and manage it |
| Reproducible | identical on laptop and droplet | depends on the host's Python/apt |
| Best for | a fresh droplet, or trying it locally | an existing box you already run this way |

Sections 3b–8 describe the bare-metal path and remain accurate. If you use Docker, they're
handled for you — skip to §0.

- [0. Docker (the recommended path)](#0-docker-the-recommended-path)
- [1. Prerequisites & accounts](#1-prerequisites--accounts)
- [2. Environment variables](#2-environment-variables)
- [3. Model keys & routing](#3-model-keys--routing)
- [3b. PostgreSQL setup](#3b-postgresql-setup) *(bare-metal only)*
- [4. Droplet setup](#4-droplet-setup) *(bare-metal only)*
- [5. Gmail authorization](#5-gmail-authorization)
- [6. Telegram bot](#6-telegram-bot)
- [7. PM2 processes](#7-pm2-processes) *(bare-metal only)*
- [8. Caddy (TLS reverse proxy)](#8-caddy-tls-reverse-proxy) *(bare-metal only)*
- [9. Laptop voice client](#9-laptop-voice-client)
- [10. Phone access](#10-phone-access)
- [11. Seeding Calvin's data](#11-seeding-calvins-data)
- [11a. Verifying a deploy](#11a-verifying-a-deploy-phase-28)
- [11b. Optional subsystems (Phases 16–22)](#11b-optional-subsystems-phases-1622)
- [12. Backups](#12-backups)
- [13. The deploy loop (laptop → droplet)](#13-the-deploy-loop)
- [14. Health & troubleshooting](#14-health--troubleshooting)

---

## 0. Docker (the recommended path)

Four files: [`Dockerfile`](../Dockerfile), [`docker-compose.yml`](../docker-compose.yml),
[`.dockerignore`](../.dockerignore), [`Caddyfile`](../Caddyfile).

```bash
git clone <your-repo> AgentOS && cd AgentOS
cp .env.example .env && nano .env         # NVIDIA_API_KEY + AGENT_WS_TOKEN at minimum (§2)
docker compose up -d --build              # postgres + api + worker + bot
docker compose ps                         # api should report (healthy) within ~30s
curl -s localhost:8000/api/health | python -m json.tool
```

That's the whole install. No venv, no apt, no PM2 — and the image is the same on your laptop
and the droplet.

### What runs

| Service | What it is | Port |
|---|---|---|
| `db` | Postgres 17, data on the `pgdata` volume | `127.0.0.1:5433` → 5432 |
| `api` | kernel + APScheduler (briefing, hunts, watchers, flip scans, Sunday recon) | `127.0.0.1:8000` |
| `worker` | drains the job queue: scraping, scoring, CV tailoring, transcription, embedding | none |
| `bot` | Telegram long-poll | none |
| `caddy` | TLS termination — **profile `tls`** | 80 / 443 |
| `tests` | the suite against a throwaway DB — **profile `test`** | none |

`api`, `worker` and `bot` are **the same image with different commands**: none depends on the
others, so `docker compose restart api` never takes the workers or the bot down.

**Heavy work runs in `worker`, never in `api`.** A 6-hourly scrape or a 60-second CV tailor
would otherwise compete with `/api/command` — the endpoint you actually talk to. Scale it:

```bash
docker compose up -d --scale worker=3      # three jobs at once
```

That is safe because claims use `FOR UPDATE SKIP LOCKED`: N workers take N different rows and
never the same one. Watch the queue:

```bash
docker compose exec api python manage.py queue                  # depth + recent failures
docker compose exec api python manage.py queue --requeue all    # retry failures after a fix
curl -s localhost:8000/api/health | python -m json.tool         # includes queue depth
```

A failed job keeps its error and is never deleted (§0 P4), so you can inspect it, fix the
cause, and requeue rather than losing the work.

`api`, `worker` and `bot` all wait for the database's healthcheck: the schema self-creates on
first connect, and a container that starts too early just crash-loops.

**Ports are bound to `127.0.0.1` deliberately.** A bare `"8000:8000"` or `"5433:5432"` listens
on `0.0.0.0`, which on a droplet publishes the kernel — and Postgres — to the open internet.
That is precisely the finding Phase 21's recon scan exists to catch. Put Caddy in front for
public access:

```bash
# DNS A record -> droplet first, then:
AGENTOS_DOMAIN=agent.example.com docker compose --profile tls up -d
```

The `bot` container **refuses to start until `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are
both set** — it fails loudly rather than run half-configured, so an unconfigured bot restart-
loops (backed off to ~60s). `docker compose up -d api db` brings up everything else without it.

### Everyday commands

```bash
docker compose logs -f api                    # or: bot
docker compose restart api                    # bot keeps running
docker compose exec api python manage.py health
docker compose exec api python manage.py hunt
docker compose exec api python manage.py infra scan
docker compose --profile test run --rm tests  # full suite, isolated database
docker compose down                           # stop (volumes, so data, survive)
```

Anything in the CLI works the same way — `docker compose exec api python manage.py <cmd>`.

### Gmail

`token.json` goes in **`secrets/`**, not the project root:

```bash
python manage.py auth                              # on the LAPTOP (it opens a browser)
scp token.json agentos@<droplet>:~/AgentOS/secrets/
```

The containers read it via `GOOGLE_TOKEN_PATH=/app/secrets/token.json`. See
[`secrets/README.md`](../secrets/README.md) for why it's a directory mount and why it's
read-write (the Gmail client rewrites the token on every hourly refresh).

### Optional ML extras

`sentence-transformers` and `faster-whisper` (droplet-side transcription for Telegram voice
notes and lecture capture) are **excluded by default** — they pull ~2GB of torch.

⚠️ **Do not install `sentence-transformers` on a small droplet.** Semantic recall no longer
needs it: since Phase 33 embeddings come from NIM-hosted `baai/bge-m3` over the network, which
is why a 961 MB / 1 CPU droplet can do semantic search at all. A local torch model there is not
merely slow, it does not fit. Without these extras the vault still embeds via NIM and voice notes
aren't transcribed. To include them:

```bash
WITH_ML=1 docker compose build && docker compose up -d
```

### Things worth knowing

- **`DATABASE_URL` in `.env` is ignored by the containers**, and must be. It holds a *host*
  DSN (`localhost:5433`); inside a container `localhost` is the container itself, so the app
  would dial itself and hang. Compose overrides it to `db:5432` via `environment:`, which wins
  over `env_file:`. Change the credentials with `POSTGRES_USER` / `POSTGRES_PASSWORD` /
  `POSTGRES_DB`, not by editing `DATABASE_URL`.
- **Never put an inline comment after a blank value in `.env`.** Write
  ```bash
  # from @BotFather
  TELEGRAM_BOT_TOKEN=
  ```
  not `TELEGRAM_BOT_TOKEN=      # from @BotFather`. python-dotenv strips that trailing comment;
  **Docker Compose's `env_file` parser does not** — with an empty value it takes the comment
  text *as the value*. That silently gave every per-task NIM key a bogus non-empty value, which
  defeats the fallback to `NVIDIA_API_KEY`. (Compose does strip the comment when a real value
  precedes it, which is why this only ever bit the blank optional vars.)
- **A managed database** just means pointing `DATABASE_URL` at it — set it under `environment:`
  for `api`/`bot`, append `?sslmode=require`, and drop the `db` service.
- **Migrating off the hand-rolled local cluster:** the compose `db` publishes on 5433, the same
  port as the no-admin cluster in `C:\tmp\agentos_pg`, so they cannot both run — you'll get
  `ports are not available: ... bind: Only one usage of each socket address`. Docker replaces
  it; dump first, because that cluster holds real data (§0 P4):
  ```bash
  pg_dump -h localhost -p 5433 -U agentos -d agentos --no-owner --no-privileges -f migrate.sql
  pg_ctl -D /c/tmp/agentos_pg stop -m fast
  docker compose up -d
  docker compose exec -T db psql -U agentos -d agentos < migrate.sql
  ```
  `relation ... already exists` errors on restore are expected and harmless — the api container
  self-creates the schema on first connect, so only the `COPY` data blocks matter. Verify with
  `docker compose exec db psql -U agentos -d agentos -c "select count(*) from jobs"`.
  Because `.env` still points at `localhost:5433`, host-side tooling (`pytest`, `psql`) keeps
  working afterwards — it just talks to the container's Postgres now.

---

## 1. Prerequisites & accounts

- **DigitalOcean droplet** — Ubuntu 22.04+, 2 GB RAM minimum (4 GB if you run
  sentence-transformers or droplet-side faster-whisper). Python 3.11+.
- **PostgreSQL 14+** — all durable state lives here (raw SQL via psycopg 3, no ORM). Can run
  on the droplet itself or as a managed DigitalOcean database. **The `pgvector` extension is
  needed** for semantic recall (Phase 33) — the Docker path uses `pgvector/pgvector:pg17`, which
  is stock Postgres 17 with the extension already present, so there is nothing to install. On a
  managed or hand-rolled database, install `pgvector` and the schema enables it on first boot.
  It degrades safely: without the extension, recall falls back to keyword search rather than
  failing, and the vector table is created outside the main schema transaction precisely so a
  missing extension cannot roll back every other table.
- **NVIDIA NIM key(s)** — free at <https://build.nvidia.com> → *Get API Key*. One is enough to
  start; see [§3](#3-model-keys--routing) for per-task keys.
- **Google Cloud project** — an OAuth **Desktop app** client for Gmail (§5).
- **Telegram** — a bot from [@BotFather](https://t.me/BotFather) and your numeric chat id.
- *(optional)* **SerpAPI** key (Google Jobs), a domain behind Cloudflare (e.g.
  `agent.example.com`) for Caddy TLS.
- *(optional)* **Africa's Talking** account — SMS, or WhatsApp with an approved sender, for
  deal-broker alerts (Phases 16–18). Leave blank and flips simply don't page you.
- *(optional)* **Spotify** — a **Premium** account plus an app at
  <https://developer.spotify.com/dashboard> for the music companion (Phase 22). Without it,
  `manage.py music <action>` fails with a clear message and nothing else is affected.
  ⚠️ **Premium and correct scopes are not sufficient for playlist creation.** A Spotify app in
  the dashboard's default quota mode gets a bare `403 Forbidden` on
  `POST /users/{id}/playlists` even with `playlist-modify-private` granted and `product=premium`
  — verified against a live account. That is an **app-level** restriction: raise the app's quota
  mode in the Developer Dashboard. Re-authorising will not fix it. (A *scope* problem reports
  itself differently — `"Insufficient client scope"` — and that one **is** fixed by re-running
  the auth. `core/spotify.py` distinguishes the two and tells you which you have.)
- *(nothing needed)* CVE lookups use **OSV.dev**, which is free and keyless.

---

## 2. Environment variables

Copy `.env.example` → `.env` and fill it in. `.env` is gitignored — never commit it.

```bash
DATABASE_URL=postgresql://agentos:PASSWORD@localhost:5432/agentos   # required
TEST_DATABASE_URL=postgresql://agentos:PASSWORD@localhost:5432/agentos_test  # tests only
NVIDIA_API_KEY=nvapi-...          # required
MY_NAME=Calvin
MY_EMAIL=you@example.com
TELEGRAM_BOT_TOKEN=...            # from @BotFather
TELEGRAM_CHAT_ID=...              # your numeric chat id (get it from @userinfobot)
AGENT_WS_TOKEN=<long-random>      # shared secret for /api/command and the voice WebSocket
# optional per-task keys (see §3) — blank = fall back to NVIDIA_API_KEY
NVIDIA_API_KEY_FAST=
NVIDIA_API_KEY_WRITE=
NVIDIA_API_KEY_CODE=
NVIDIA_API_KEY_RESEARCH=
# optional — Africa's Talking, for deal-broker alerts (Phases 16-18)
AT_USERNAME=                      # 'sandbox' for testing
AT_API_KEY=
AT_PHONE=                         # your number in E.164, e.g. +2547XXXXXXXX
AT_WHATSAPP_FROM=                 # set BOTH WhatsApp vars to enable it; blank = SMS fallback
AT_WHATSAPP_ENDPOINT=             # account-specific, hence configurable
# optional — Spotify (Phase 22). Premium required.
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
SPOTIFY_REFRESH_TOKEN=            # from `manage.py music connect` — env only, never the DB
# optional — SerpAPI (Google Jobs source). Blank = that source is simply skipped.
SERPAPI_KEY=
# server
AGENTOS_HOST=0.0.0.0
AGENTOS_PORT=8000
AGENTOS_TZ=Africa/Nairobi
```

`AGENTOS_TZ` is load-bearing beyond cosmetics: the music companion resolves rules like
"never queue explicit lyrics before 8am" against **this** timezone, not the droplet's clock.
A UTC droplet would otherwise fire it three hours out.

Non-secret settings (model routes, job profile, event tags, voices, flip thresholds, recon
targets) live in `config.yaml` and are safe to commit.

**`config/timetable.yaml` is gitignored** — a real timetable says where you physically are all
week, which doesn't belong in a public repo. Copy the example and fill in your own; a missing
file is handled (the briefing just reports no classes):

```bash
cp config/timetable.example.yaml config/timetable.yaml
```

Since it isn't in git, `git pull` on the droplet won't carry it — `scp` it across once, the
same as `.env` and `secrets/token.json`.

> `TEST_DATABASE_URL` in `.env` **overrides** the test suite's built-in default — `conftest.py`
> reads it after `.env` has been loaded. Point it at a host that isn't listening and the suite
> stalls on its first database test rather than failing fast.

---

## 3. Model keys & routing

AgentOS uses the **best model per task class** and can spread concurrent work across
**separate API keys**. Task classes → models are defined in `config.yaml → llm.routes`:

```yaml
routes:
  classify:    { model: "meta/llama-3.1-8b-instruct",     api_key_env: NVIDIA_API_KEY_FAST, temperature: 0.0 }
  write:       { model: "mistralai/mistral-medium-3.5-128b", api_key_env: NVIDIA_API_KEY_WRITE }
  persona:     { model: "mistralai/mistral-medium-3.5-128b", api_key_env: NVIDIA_API_KEY_WRITE }
  code_review: { model: "deepseek-ai/deepseek-v4-pro", api_key_env: NVIDIA_API_KEY_CODE }
  research:    { model: "nvidia/nemotron-3-super-120b-a12b", api_key_env: NVIDIA_API_KEY_RESEARCH, max_tokens: 1500 }
  voice_chat:  { model: "meta/llama-3.1-8b-instruct",     api_key_env: NVIDIA_API_KEY_FAST }
```

**Do you need multiple keys?** No — leave the `NVIDIA_API_KEY_*` vars blank and everything uses
`NVIDIA_API_KEY`. Add them when you notice throttling: because the scheduler, the Telegram bot,
and voice all call the LLM concurrently, giving `classify`/`write`/`code_review`/`research`
their own keys puts them on separate rate-limit buckets so a burst of one can't starve another.
Obtain extra free NIM keys (or point a task's `base_url` at another NIM-compatible endpoint).

To change a model, edit one line in `config.yaml` — no code changes, no restart of logic.

---

## 3b. PostgreSQL setup

All state (jobs, applications, persona facts, flashcards, deadlines, vault vectors, the flip
pipeline) lives in Postgres. Raw SQL via psycopg 3 — no ORM.

```bash
# on the droplet
sudo apt install -y postgresql postgresql-client
sudo -u postgres psql <<'SQL'
CREATE USER agentos WITH PASSWORD 'change-me';
CREATE DATABASE agentos      OWNER agentos;
CREATE DATABASE agentos_test OWNER agentos;   -- only needed if you run the tests there
SQL
```

Then set `DATABASE_URL` in `.env`. The schema **creates itself on first run** — every table
is `CREATE TABLE IF NOT EXISTS`, and column additions use `ADD COLUMN IF NOT EXISTS`, so
deploys are safe to re-run and never drop anything.

```bash
python manage.py health        # "db_ok": true confirms the connection + schema
```

A managed Postgres works too — just point `DATABASE_URL` at it (append `?sslmode=require`).

**Local development:** if you can't run the system Postgres service, you can start a
user-owned cluster on a spare port without admin rights:

```bash
initdb -D /tmp/agentos_pg -U agentos --auth=trust
pg_ctl -D /tmp/agentos_pg -o "-p 5433" -l /tmp/agentos_pg/server.log start
createdb -h localhost -p 5433 -U agentos agentos
createdb -h localhost -p 5433 -U agentos agentos_test
# DATABASE_URL=postgresql://agentos:agentos@localhost:5433/agentos
```

On a non-default port like this, set **both** `DATABASE_URL` **and** `TEST_DATABASE_URL` in
`.env`. The test suite reads `TEST_DATABASE_URL` from the environment, and `.env` wins over its
built-in default — a leftover `:5432` there points the whole suite at a cluster that isn't there.

---

## 4. Droplet setup

```bash
ssh root@<droplet-ip>
apt update && apt install -y python3.11 python3.11-venv git tesseract-ocr postgresql postgresql-client
adduser --disabled-password agentos && su - agentos

git clone <your-repo> AgentOS && cd AgentOS
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# optional, higher-quality / extra capabilities (heavier):
# NOT needed for semantic search — that runs on NIM (Phase 33) and does not fit a small droplet:
# pip install sentence-transformers
pip install faster-whisper             # droplet-side lecture + Telegram voice-note transcription

cp .env.example .env && nano .env       # fill in §2
mkdir -p logs data
python manage.py health                 # sanity check (no network needed)
```

`tesseract-ocr` is only needed if you want OCR of note *images* in the study vault.

---

## 5. Gmail authorization

Do this **on the laptop** (it opens a browser), then copy the token to the droplet.

1. Google Cloud Console → APIs & Services → enable **Gmail API**.
2. Create an OAuth client → **Desktop app** → download `credentials.json` into the project root.
3. On the laptop: `python manage.py auth` → sign in → it writes `token.json`.
4. Copy `token.json` to the droplet's project root (`scp token.json agentos@<ip>:~/AgentOS/`).

Scopes: `gmail.modify` (read/label/archive/draft — *cannot* permanently delete) and `gmail.send`
(used **only** by `core/mailer.py` for approved job applications). Email replies never send.

---

## 6. Telegram bot

1. @BotFather → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) → copy your numeric id into `TELEGRAM_CHAT_ID`.
   Only this chat is authorized; every handler checks it.
3. The bot runs as its own PM2 process (§7). Test locally first: `python manage.py telegram`,
   then send `/status`.

---

## 7. PM2 processes

The API (kernel + scheduler), the queue worker, and the Telegram bot are **independent**
processes so one can restart without the others. `ecosystem.config.js` defines all three:

```bash
npm install -g pm2
pm2 start ecosystem.config.js     # agentos-api + agentos-worker + agentos-bot
pm2 logs agentos-api
pm2 save && pm2 startup           # survive reboots (run the printed command)
```

- `agentos-api` → `uvicorn kernel.app:app` on `:8000` (also runs APScheduler: cleanup,
  briefing, watchers, digests — and **enqueues** the heavy jobs rather than running them).
- `agentos-worker` → `python -m kernel.worker`, drains `job_queue`.
- `agentos-bot` → `manage.py telegram` (long-poll).

> **`agentos-worker` is not optional.** Since Phase 26 the scheduler enqueues the heavy jobs
> — `job_hunter.hunt`, `vault.ingest`, `lecture.inbox`, `flip.scan`, `events.scan`,
> `proactive.triage` — instead of running them inline. With no worker those rows are claimed
> by nobody: the hunt quietly stops finding jobs and the 05:30 triage never runs, with nothing
> in the logs to say so, because the *enqueue* succeeded. This is the one PM2/Docker
> difference that fails silently rather than loudly.

Scale it the same way compose does — claims use `FOR UPDATE SKIP LOCKED`, so N workers take
N different rows and never the same one:

```bash
pm2 scale agentos-worker 3
python manage.py queue            # depth + recent failures
```

Adjust the venv path in `ecosystem.config.js` (`./.venv/bin/python`) if yours differs.

---

## 8. Caddy (TLS reverse proxy)

To expose the voice WebSocket (`/ws/voice`) and `/api/*` over HTTPS/WSS, put Caddy in front:

```
# /etc/caddy/Caddyfile
agent.example.com {
    reverse_proxy localhost:8000
}
```

```bash
apt install -y caddy && systemctl reload caddy
```

Caddy auto-provisions TLS. Point your Cloudflare DNS `A`/`AAAA` record at the droplet. The
laptop client then uses `AGENT_WS_URL=wss://agent.example.com/ws/voice`.

---

## 9. Laptop voice client

Runs on Calvin's laptop, not the droplet. Full guide: [`client/README.md`](../client/README.md).

```bash
cd AgentOS
python -m venv .venv && source .venv/bin/activate   # (or Windows equivalent)
pip install -r client/requirements.txt
export AGENT_WS_URL="wss://agent.example.com/ws/voice"
export AGENT_WS_TOKEN="<same AGENT_WS_TOKEN as the droplet>"
python client/voice_client.py          # wake-word mode ("Hey Agent, …")
python client/voice_client.py --ptt    # push-to-talk (noisy rooms)
```

Autostart on boot: files in `client/autostart/` (systemd `.service`, launchd `.plist`, Windows
`.bat`) — edit paths + token, then enable. **Only pre-built edge-tts voices are used; there is
no voice-cloning path.**

---

## 9a. Desktop HUD (Phase 36)

The JARVIS-style HUD — same backend, a frameless always-on-top window instead of the
tkinter tray window above. They're independent entry points; run whichever one you want
(or neither — the web dashboard at `/dashboard` needs no laptop client at all).

```bash
pip install -r client/requirements.txt   # adds pywebview, pynput, screeninfo to the Phase 7 set
export AGENT_WS_URL="wss://agent.example.com/ws/voice"
export AGENT_WS_TOKEN="<same AGENT_WS_TOKEN as the droplet>"
python client/hud_window.py --tray                # start hidden, tray icon is the way in
python client/hud_window.py --compact             # start as the small corner ring
```

Optional environment variables (see `.env.example`):

- `AGENT_HUD_HOTKEY` — global summon/dismiss hotkey, default `<ctrl>+<alt>+j`. Works root-free
  on Windows, macOS, and X11; **Wayland has no portable global-hotkey API**, and
  `client/hud_window.py` reports the hotkey as unavailable rather than silently doing nothing.
- `AGENT_CLIENT_MODE=voice` — folds the wake word into the HUD (opt-in, same gate Phase 24
  established: nobody opts *out* of a hot microphone, so nobody is opted in by default).
  Saying the wake word shows the window and opens a listening session; it does not record
  anything before that.

The HUD serves `frontend/` over a loopback-only HTTP server on an ephemeral port (not
`file://` — Chromium's WebView2 backend blocks relative ES module imports under that scheme)
and points its own API/WebSocket calls at `AGENT_WS_URL` via a `?server=` query parameter on
that local URL. Nothing about this reaches the internet to serve the page; only the actual
API/voice traffic goes to the droplet, exactly as it already did for `voice_client.py`.

---

## 10. Phone access

No app to install — two paths reuse the same backend:

1. **Telegram voice notes** — send a voice note to the bot; it's transcribed on the droplet
   (faster-whisper) and routed through the same intent engine, replying as text. Most reliable.
2. **Push-to-talk shortcut** — an iOS Shortcut / Android tap that POSTs to
   `https://agent.example.com/api/command` with `{"text": "<words>"}` and the HTTP
   header `Authorization: Bearer <AGENT_WS_TOKEN>`. Requests without the shared token fail
   closed; never put the token in the URL.

---

## 10a. Android phone control — ADB (Phase 36)

**This is Calvin's own Android device, and requires his explicit authorization on that
device** — the trust prompt below is not a formality, it's the actual consent gate. Nothing
here works without it, by design.

1. **Enable Developer Options and USB debugging** on the phone: Settings → About phone → tap
   "Build number" 7 times → Developer options → enable "USB debugging".
2. **Connect it to the laptop that runs `client/hud_window.py`** — over USB, or wirelessly:
   ```bash
   adb pair <phone-ip>:<pairing-port>          # code shown on the phone's
                                                # Developer options → Wireless debugging screen
   adb connect <phone-ip>:<port>
   ```
3. **Accept the trust prompt on the phone itself** the first time — "Allow USB debugging?
   ... Always allow from this computer". Nothing works before this is accepted; there is no
   way around it, and there shouldn't be.
4. Verify: `adb devices` should list the phone as `device` (not `unauthorized` — go back and
   accept the prompt if it says that, or `offline` — unplug/replug or re-pair).

What this does and does not enable:

- **Calls are placed via Android intents and keyevents only** (`am start -a
  android.intent.action.CALL`, `input keyevent`) — never pixel-coordinate taps, so it
  survives a phone screen resolution or launcher change untouched.
- **Placing a call is `high` tier and asks every time** — see §2 of `ARCHITECTURE.md`'s
  Phase 36 section. There is no setting that makes it stop asking.
- **Answering/ending an already-ringing call is `low` tier** and learnable — it acts on
  something already happening, not something initiated in Calvin's name.
- The laptop (`client/adb_bridge.py`) re-validates every request against strict E.164 and its
  own device list independently of whatever the droplet asked for — the same "the laptop has
  the final say" boundary Phase 23 established for desktop app control.

---

## 11. Seeding Calvin's data

Once running, teach it about Calvin:

```bash
python manage.py persona-init                 # interactive → verified persona facts
# put the master CV at data/cv/master_cv.pdf (or .docx/.md), then:
python manage.py cv-update                    # parse into cv_facts (diff + persona cross-check)
# course notes:  data/vault/<UNIT_CODE>/*.pdf|pptx|docx|png ...
python manage.py ingest                       # embed into the study vault
# lectures:  data/lectures/inbox/<UNIT>__name.mp3
python manage.py lecture                       # → notes + flashcards + PDF
cp config/timetable.example.yaml config/timetable.yaml   # then fill in your real classes
# tune config.yaml too (job profile, interest tags, commitments)
```

Job hunting, briefings, quizzes, tutoring, events, and CV tailoring all work off this data.

---

## 11a. Verifying a deploy (Phase 28)

`pytest` tells you a number; this tells you **which capability** broke, on your phone, as each
group finishes:

```bash
docker compose exec api python manage.py selftest        # all services, ✅/❌ to Telegram
docker compose exec api python manage.py selftest "job hunter"   # substring match
docker compose exec api python manage.py selftest --no-send        # print instead of Telegram
```

It never fabricates a pass: a group that errors, times out, or whose runner falls over reports
❌ with the reason rather than a hollow green.

---

## 11b. Optional subsystems (Phases 16–22)

All four are inert until you configure them — the rest of AgentOS runs fine without any.

### Deal broker (16–18)

Sources are **disabled by default**. Enable them in `config.yaml → flip.sources` and tune the
thresholds (`min_price_gap_pct`, `min_listing_age_days`, `max_velocity_days`, `min_score`,
`flash_window_hours`, `price_drop_tiers`, `platform_fee_pct`, `category_velocity_days`).

```bash
python manage.py flip scan          # one sourcing pass
python manage.py flip report        # margin ledger
```

Read this before enabling: AgentOS **cannot spend money** — there is no payment integration, and
`confirm_purchase` refuses without a passing purchase gate. It **cannot message anyone** — it
drafts, you send. **Facebook Marketplace is deliberately not scraped**; Meta pursues scrapers.
If you add a source, keep it on that side of the line.

### Self-audit / infra recon (21)

Enrol only hosts **you own**. Nothing is scanned until you do — the skill has no target list by
default and no way to act on what it finds.

```bash
python manage.py infra enroll agent.example.com --ports 80,443
python manage.py infra targets
python manage.py infra scan          # also runs weekly, Sun 06:00
```

Anything open that isn't in `--ports` becomes a finding. Tune `infra.tls_warn_days` and
`infra.probe_ports` in `config.yaml`. The CVE check takes package **names** from
`requirements.txt` and their **versions from what's actually installed** in the running
environment — a requirement that isn't installed is skipped rather than guessed at. Run it in
the same virtualenv the droplet serves from, or it will audit the wrong versions. Note it covers
the packages we *declare*, not transitive dependencies. On the droplet, PM2/Docker health checks
need those binaries on `PATH`.

### Music (22)

Requires **Spotify Premium**. Create an app in the Spotify dashboard and set `SPOTIFY_CLIENT_ID`
/ `SPOTIFY_CLIENT_SECRET`, then:

```bash
# 1. First run (no token yet) = the one-time consent flow.
python manage.py music connect
#    It prints the redirect URI to register on your app + a consent URL. Approve it, land on a
#    page that won't load, paste that URL back. It prints SPOTIFY_REFRESH_TOKEN for your .env.
#    The redirect defaults to http://127.0.0.1:8888/callback; override it to match a URI you
#    have already registered:
python manage.py music connect --redirect http://127.0.0.1:9999/cb

# 2. With the token in .env, the same command becomes a health check.
python manage.py music connect       # confirms the account + warns if it isn't Premium
python manage.py music taste         # builds the taste model (also runs weekly, Sun 04:00)
python manage.py music now
```

It's paste-based rather than a local callback server, so it works identically over SSH on the
droplet. The refresh token lives in the **environment, never the database** — nothing writes it
anywhere; you copy it into `.env` yourself. Spotify has removed
Recommendations / Audio Features / Audio Analysis / Related Artists / Featured Playlists for
apps created now, and the Web API cannot mix audio at any tier — so don't expect BPM data or
real crossfading; see [`core/spotify.py`](../core/spotify.py)'s docstring before adding endpoints.

### Adaptive layer (20)

On by default and **passive** — it observes, then proposes. Nothing it notices changes behaviour
until you confirm it (`/rules`, `/retro`, `/contracts` in Telegram, or `manage.py adaptive`).
`config.yaml → adaptive.threshold` (default 4) is how many times a pattern must repeat, with no
contradicting instance, before it's worth proposing.

---

## 12. Backups

Your state is in **two** places: the Postgres database and the `data/` files (vault, CV
variants, prep/lecture PDFs). `manage.py backup` captures both — a `pg_dump` of the database
plus the files — into one archive:

```bash
python manage.py backup            # → agentos-backup-<timestamp>.tar.gz  [database + files]
```

It needs `pg_dump` on PATH (`postgresql-client`) and fails loudly if it's missing rather than
silently writing a files-only archive. Restore with:

```bash
tar -xzf agentos-backup-<timestamp>.tar.gz
psql -d "$DATABASE_URL" -f agentos.sql        # database
cp -r data /path/to/AgentOS/                  # files
```

Add a cron entry (and optionally `scp`/`s3cmd` the tarball offsite, e.g. DigitalOcean Spaces):

```cron
# crontab -e  (as the agentos user)
30 2 * * *  cd /home/agentos/AgentOS && ./.venv/bin/python manage.py backup >> logs/backup.log 2>&1
```

Nothing in AgentOS ever deletes data (§0 Principle 4); backups protect against disk loss.

---

## 13. The deploy loop

Match Calvin's existing infra pattern — push from the laptop, pull + restart on the droplet:

```bash
# laptop
git add -A && git commit -m "…" && git push

# droplet
cd ~/AgentOS && git pull && pm2 restart agentos-api agentos-worker agentos-bot
pm2 logs --lines 50        # verify
curl -s localhost:8000/api/health | python -m json.tool
```

---

## 14. Health & troubleshooting

- **`GET /api/health`** (or `python manage.py health`) reports scheduler, DB, NIM-key presence,
  Gmail-token status, Telegram config, and discovered skills.
- **`connection refused` / `db_ok: false`** — Postgres isn't running or `DATABASE_URL` is
  wrong. Check with `pg_isready` and `psql "$DATABASE_URL" -c "select 1"`.
- **`could not connect to postgresql://…`** — `Memory` gives up after 10s and names the host
  (password redacted). A DSN pointing at a port that *drops* rather than refuses (a firewall,
  a stale Docker proxy) would otherwise hang the process forever, which looks like a slow
  start-up rather than a misconfiguration.
- **`No API key resolved`** — set `NVIDIA_API_KEY` in `.env` (and per-task keys if you reference
  them in `config.yaml`).
- **`Missing token.json`** — run the Gmail flow (§5) on the laptop and copy the token over.
- **Telegram silent** — check `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`; only the one chat id works.
- **Vault answers weak / lexical** — recall has silently fallen back to keyword search. Check
  that the `pgvector` extension exists on the database and that `NVIDIA_API_KEY` is set, since
  `config.yaml → vault.embedder: auto` resolves to the NIM embedder first (Phase 33). Do **not**
  install `sentence-transformers` to fix this on a small droplet — it will not fit.
- **Voice notes not transcribed** — install `faster-whisper` on the droplet.
- **A scraper source errors** — logged and skipped; the hunt continues (one bad source never
  aborts a run).
- **Docker: `bot` restart-loops with "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set"** —
  working as intended: it refuses to run half-configured rather than pretend. Set both in `.env`
  (§6) and `docker compose up -d bot`. Docker backs the restarts off to ~60s, so it's noisy but
  harmless; `docker compose up -d api db` runs everything else without it.
- **Docker: build fails with `Redirection loop encountered` / a carrier URL during `apt-get`** —
  a captive or zero-rated mobile network is hijacking plain HTTP. The Dockerfile already points
  apt at **https://** mirrors for this reason; if you add an apt source, use HTTPS too.
- **Docker: builds die mid-step with `rpc error ... EOF`, or `docker system df` reports a
  missing snapshot** — the containerd store is corrupt, and on Windows that usually means the
  disk filled up. Check free space first; `docker builder prune -af` clears the cache, and
  `wsl --unregister docker-desktop` rebuilds the engine from scratch (destroys images/volumes).
- **`SPOTIFY_REFRESH_TOKEN is not set`** — run `manage.py music connect` (with no token set it
  runs the consent flow) and copy the printed token into `.env`. Every other subsystem is
  unaffected.
- **Spotify `INVALID_CLIENT: Invalid redirect URI`** — the `--redirect` value must match a URI
  registered on your app *exactly*. Spotify requires `127.0.0.1` for loopback, not `localhost`.
- **Spotify `403`** — almost always a non-Premium account; sometimes a missing scope. If you
  asked for a deprecated endpoint, the client raises before the request is even sent.
- **Music rule firing at the wrong hour** — check `AGENTOS_TZ`. Rules resolve against Calvin's
  timezone via `zoneinfo`, so `tzdata` must be installed (it's in `requirements.txt`).
- **Flip alerts silent** — set `AT_USERNAME`/`AT_API_KEY`/`AT_PHONE`. WhatsApp additionally needs
  **both** `AT_WHATSAPP_FROM` and `AT_WHATSAPP_ENDPOINT`; with either missing it falls back to SMS.
- **`infra scan` finds nothing** — you probably haven't enrolled a target. That's the design:
  it scans nothing you haven't explicitly enrolled.
- **`infra scan` slow** — each enrolled host with an open 80/443 is probed for sensitive paths
  with retry+backoff. Hosts with no web port skip HTTP entirely.
- **Logs** — `logs/agentos.log` (rotating), plus `pm2 logs agentos-api|agentos-worker|agentos-bot`.
