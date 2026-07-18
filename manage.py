#!/usr/bin/env python
"""AgentOS management CLI.

A single entrypoint for operating AgentOS from the laptop or droplet. Phase 1 implements
serve / health / command / backup; later-phase subcommands (auth, persona-init, hunt,
digest, approve, cleanup, summary, ask, review, cv-update) are registered as stubs that
announce which phase will fill them in, so the CLI surface is stable from day one.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
import time
from pathlib import Path

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("manage")

_FUTURE: dict[str, str] = {}


def cmd_infra(args: argparse.Namespace) -> int:
    """Self-audit enrolled infra. Report-only — it never restarts or patches anything."""
    from skills.infra_recon import SKILL

    if args.action == "enroll":
        r = SKILL.enroll(target=args.target or "", ports=args.ports or "")
    elif args.action == "targets":
        r = SKILL.targets()
    elif args.action == "scan":
        r = SKILL.scan(notify=not args.no_send)
    else:
        r = SKILL.report(notify=not args.no_send)
    print(r.text)
    return 0 if r.ok else 1


def _spotify_authorize(redirect_uri: str) -> int:
    """One-time consent flow → prints the refresh token for .env.

    Interactive and paste-based rather than a local callback server, so it works the same over
    SSH on the droplet as it does on the laptop. The token is printed once and never stored by
    us — it belongs in .env, never in the database.
    """
    from core.spotify import SpotifyClient, SpotifyError

    client = SpotifyClient()
    try:
        url = client.authorize_url(redirect_uri)
    except SpotifyError as exc:
        print(exc)
        return 1
    print("1. Add this EXACT redirect URI to your app at "
          "https://developer.spotify.com/dashboard:\n"
          f"   {redirect_uri}\n"
          "2. Open this URL and approve:\n"
          f"   {url}\n"
          "3. You'll land on a page that won't load — that's expected. Copy the whole URL\n"
          "   from the address bar (or just the `code=` value) and paste it below.\n")
    raw = input("Redirect URL or code: ").strip()
    code = raw
    if "code=" in raw:
        from urllib.parse import parse_qs, urlparse

        code = parse_qs(urlparse(raw).query).get("code", [""])[0]
    if not code:
        print("No code found in that input.")
        return 1
    try:
        token = client.exchange_code(code, redirect_uri)
    except SpotifyError as exc:
        print(f"Could not exchange the code: {exc}")
        return 1
    print("\nAdd this to .env (it is not stored anywhere else):\n\n"
          f"SPOTIFY_REFRESH_TOKEN={token}\n\n"
          "Then run `python manage.py music connect` again to verify.")
    return 0


def cmd_music(args: argparse.Namespace) -> int:
    """Spotify control. DJ mode = smart sequencing + narration, not audio mixing."""
    import os

    from skills.music import SKILL

    a = args.action
    if a == "connect":
        # No refresh token yet -> this is the bootstrap, not a health check.
        if not os.getenv("SPOTIFY_REFRESH_TOKEN"):
            return _spotify_authorize(args.redirect)
        r = SKILL.connect()
    elif a == "taste":
        r = SKILL.taste()
    elif a == "queue":
        r = SKILL.auto_queue(cue=args.cue or "")
    elif a == "playlist":
        r = SKILL.playlist(theme=args.cue or "")
    elif a == "discover":
        r = SKILL.discover()
    elif a == "dj":
        r = SKILL.dj(cue=args.cue or "")
    elif a == "devices":
        r = SKILL.devices(transfer_to=args.cue or "")
    elif a == "volume":
        r = SKILL.volume(percent=int(args.cue or 50))
    elif a == "now":
        r = SKILL.now_playing()
    else:
        r = {"play": SKILL.play, "pause": SKILL.pause,
             "next": SKILL.next_track, "previous": SKILL.previous_track}[a]()
    print(r.text)
    return 0 if r.ok else 1


def cmd_adaptive(args: argparse.Namespace) -> int:
    """Patterns AgentOS has noticed, the rules it proposes, and each skill's declared scope."""
    from skills.adaptive import SKILL

    a = args.action
    if a == "candidates":
        r = SKILL.candidates()
    elif a == "propose":
        r = SKILL.propose(notify=not args.no_send)
    elif a == "confirm":
        r = SKILL.confirm(signal_id=args.id)
    elif a == "decline":
        r = SKILL.decline(signal_id=args.id)
    elif a == "retro":
        r = SKILL.retro(answer=args.answer or "", notify=not args.no_send)
    else:
        r = SKILL.contracts()
    print(r.text)
    return 0 if r.ok else 1


def cmd_flip(args: argparse.Namespace) -> int:
    """Drive the flash-flip pipeline. AgentOS never spends money or messages sellers —
    `draft` writes a message for you to send, and `purchase` only records a purchase that an
    approved gate (committed buyer + confirmed availability + your approval) already cleared."""
    from skills.deal_broker import SKILL

    a = args.action
    if a == "scan":
        r = SKILL.scan()
    elif a == "pipeline":
        r = SKILL.pipeline()
    elif a == "digest":
        r = SKILL.digest(notify=not args.no_send)
    elif a == "expire":
        r = SKILL.expire_stale(notify=not args.no_send)
    elif a == "draft":
        r = SKILL.draft_negotiation(listing_id=args.id)
    elif a == "agree":
        r = SKILL.record_agreement(listing_id=args.id, price=args.price, thread_id=args.thread or 0)
    elif a == "list":
        r = SKILL.list_item(listing_id=args.id, resale_price=args.price,
                            platform=args.platform, notify=not args.no_send)
    elif a == "buyer":
        r = SKILL.buyer_found(listing_id=args.id, handle=args.handle or "buyer",
                              platform=args.platform, committed=args.committed, paid=args.paid,
                              amount=args.price, notify=not args.no_send)
    elif a == "gate":
        r = SKILL.purchase_gate(listing_id=args.id, availability_confirmed=args.available,
                                approve=args.approve)
    elif a == "purchase":
        r = SKILL.confirm_purchase(listing_id=args.id)
    elif a == "delivered":
        r = SKILL.mark_delivered(listing_id=args.id)
    elif a == "inquiry":
        r = SKILL.inquiry(listing_id=args.id, platform=args.platform,
                          handle=args.handle or "buyer", message=args.message or "")
    elif a == "stats":
        r = SKILL.record_stats(listing_id=args.id, platform=args.platform,
                               views=args.views, inquiries=args.inquiries)
    elif a == "ledger":
        r = SKILL.ledger(listing_id=args.id)
    elif a == "report":
        r = SKILL.margin_report(days=args.days, notify=not args.no_send)
    elif a == "reject":
        r = SKILL.reject_flip(listing_id=args.id, reason=args.message or "")
    else:
        print(f"Unknown flip action '{a}'.")
        return 1
    print(r.text)
    return 0 if r.ok else 1


def cmd_cv_update(_: argparse.Namespace) -> int:
    """Ingest data/cv/master_cv.* into structured cv_facts (with a diff + persona cross-check)."""
    from skills.cv_tailor import SKILL

    print(SKILL.update().text)
    return 0


def cmd_cv(args: argparse.Namespace) -> int:
    """Show CV status/variants, or tailor to a pasted job description."""
    from skills.cv_tailor import SKILL

    if args.tailor:
        r = SKILL.tailor(target=args.tailor, company=args.company or "")
    elif args.facts:
        r = SKILL.facts()
    else:
        r = SKILL.view()
    print(r.text)
    return 0 if r.ok else 1


def cmd_review(args: argparse.Namespace) -> int:
    """Tutor-style review of a code file (teaching feedback, not a rewrite unless --rewrite)."""
    from pathlib import Path as _P

    from skills.code_tutor import SKILL

    code = _P(args.file).read_text(encoding="utf-8", errors="ignore")
    result = SKILL.review(code=code, unit=args.unit or "", rewrite=args.rewrite)
    print(result.text)
    return 0 if result.ok else 1


def cmd_tutor(args: argparse.Namespace) -> int:
    """Interactive tutor session (explain/drill/socratic/mock lab)."""
    from skills.code_tutor import SKILL

    res = SKILL.start(topic=args.topic)
    print(res.text)
    # drill/socratic/mocklab are interactive; explain/review are one-shot
    while SKILL._session() and SKILL._session().get("mode") in ("drill", "socratic", "mocklab"):
        try:
            reply = input("\n> ").strip()
        except EOFError:
            break
        if not reply or reply.lower() in ("quit", "exit", "end"):
            SKILL.end()
            break
        res = SKILL.continue_session(text=reply)
        print("\n" + res.text)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Ingest course materials into the study vault."""
    from skills.study_vault import SKILL

    result = SKILL.ingest(unit=args.unit or "")
    print(result.text)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Ask a question against the study vault (cites file + page/slide)."""
    from skills.study_vault import SKILL

    result = SKILL.ask(question=args.question, unit=args.unit or "", allow_web=not args.no_web)
    print(result.text)
    return 0 if result.ok else 1


def cmd_vault(args: argparse.Namespace) -> int:
    """Show vault status."""
    from skills.study_vault import SKILL

    print(SKILL.status().text)
    return 0


def cmd_quiz(args: argparse.Namespace) -> int:
    """Interactive spaced-repetition review in the terminal."""
    from skills.spaced_rep import SKILL

    res = SKILL.quiz(unit=args.unit or "")
    print(res.text)
    while SKILL.session_active():
        try:
            input("\n(press Enter to reveal) ")
        except EOFError:
            break
        print(SKILL.reveal().text)
        grade = input("grade [again/hard/good/easy]> ").strip().lower()
        res = SKILL.grade(grade=grade)
        print("\n" + res.text)
    return 0


def cmd_cards(args: argparse.Namespace) -> int:
    """List candidate flashcards, or approve/reject one by id."""
    from skills.spaced_rep import SKILL

    if args.approve:
        print(SKILL.approve_card(card_id=args.approve).text)
    elif args.reject:
        print(SKILL.reject_card(card_id=args.reject).text)
    else:
        print(SKILL.list_candidates(unit=args.unit or "").text)
    return 0


def cmd_review_report(args: argparse.Namespace) -> int:
    """Weekly spaced-repetition report."""
    from skills.spaced_rep import SKILL

    print(SKILL.report(notify=not args.no_send).text)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """Find free events matching Calvin's interests, or edit interest tags."""
    from skills.event_scout import SKILL

    if args.tags_action:
        r = SKILL.tags(action=args.tags_action, tag=args.tag or "")
    else:
        r = SKILL.find(tag=args.tag or "")
    print(r.text)
    return 0 if r.ok else 1


def cmd_briefing(args: argparse.Namespace) -> int:
    """Print (or send) the unified morning briefing."""
    from skills.semester_planner import SKILL

    print(SKILL.briefing(notify=not args.no_send).text)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    """Propose the week plan."""
    from skills.semester_planner import SKILL

    print(SKILL.plan(notify=not args.no_send).text)
    return 0


def cmd_cram(args: argparse.Namespace) -> int:
    """Panic mode: revision schedule + mock CAT for a unit."""
    from skills.semester_planner import SKILL

    result = SKILL.cram(unit=args.unit, days=args.days, notify=not args.no_send)
    print(result.text)
    if result.data.get("cat_pdf"):
        print(f"\nCAT paper: {result.data['cat_pdf']}")
    return 0 if result.ok else 1


def cmd_deadline(args: argparse.Namespace) -> int:
    """Add a deadline or list what's due."""
    from skills.semester_planner import SKILL

    if args.title and args.due:
        r = SKILL.deadline_add(title=args.title, due=args.due, unit=args.unit or "",
                               dtype=args.type or "", weight=args.weight)
    else:
        r = SKILL.due(days=args.days)
    print(r.text)
    return 0 if r.ok else 1


def cmd_lecture(args: argparse.Namespace) -> int:
    """Process a lecture audio file (or the whole inbox) into notes + flashcards."""
    from skills.lecture_capture import SKILL

    if args.path:
        result = SKILL.capture(path=args.path, unit=args.unit or "", notify=not args.no_send)
    else:
        result = SKILL.process_inbox()
    print(result.text)
    return 0 if result.ok else 1


def cmd_persona_init(_: argparse.Namespace) -> int:
    """Run the interactive persona seeding interview."""
    from core.persona_init import PersonaInterview

    PersonaInterview().run()
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    """Inspect the job queue: depth, recent failures, requeue after a fix."""
    from core.queue import get_queue

    q = get_queue()
    if args.requeue:
        n = q.requeue_failed(None if args.requeue == "all" else args.requeue)
        print(f"Requeued {n} failed job(s).")
        return 0
    stats = q.stats()
    print("Queue: " + "  ".join(f"{k}={v}" for k, v in stats.items()))
    fails = q.recent_failures()
    if fails:
        print("\nRecent failures:")
        for f in fails:
            print(f"  [{f['id']}] {f['kind']} (x{f['attempts']}): {str(f['last_error'])[:110]}")
    return 0


def cmd_persona_github(args: argparse.Namespace) -> int:
    """Derive CANDIDATE persona facts from Calvin's public repos (he confirms each)."""
    from skills.persona import SKILL

    if getattr(args, "detailed", False):
        res = SKILL.import_github_detailed(user=args.user or "", notify=not args.no_send)
    else:
        res = SKILL.import_github(user=args.user or "", notify=not args.no_send)
    print(res.text)
    return 0 if res.ok else 1


def cmd_persona_facts(args: argparse.Namespace) -> int:
    """List candidate facts awaiting confirmation, or confirm/reject one."""
    from skills.persona import SKILL

    if args.verify:
        cat, _, key = args.verify.partition(".")
        res = SKILL.verify(category=cat, key=key, accept=not args.reject)
    else:
        res = SKILL.candidates()
    print(res.text)
    return 0 if res.ok else 1


def cmd_form(args: argparse.Namespace) -> int:
    """Build a review-ready answer sheet from pasted questions or a form URL (never submits)."""
    from skills.form_assist import SKILL

    result = SKILL.answer(content=args.text or "", url=args.url or "")
    print(result.text)
    return 0 if result.ok else 1


def cmd_telegram(_: argparse.Namespace) -> int:
    """Run the Telegram bot (blocking long-poll). Launch as its own PM2 process."""
    from skills.telegram_bot import run

    run()
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    """Show the current voice, list options, or switch to a pre-built voice."""
    from skills.voice import SKILL

    if args.alias:
        result = SKILL.set_voice(voice=args.alias)
    else:
        cur = SKILL.get_voice()
        lst = SKILL.list_voices()
        print(cur.text)
        print(lst.text)
        return 0
    print(result.text)
    return 0 if result.ok else 1


def cmd_research(args: argparse.Namespace) -> int:
    """Search the web and synthesize a cited answer."""
    from skills.research import SKILL

    result = SKILL.search(query=args.query, deliver_full=not args.no_send)
    print(result.text)
    for s in result.data.get("sources", []):
        print(f"  [{s['n']}] {s['title']} — {s['url']}")
    return 0 if result.ok else 1


def cmd_prep(args: argparse.Namespace) -> int:
    """Generate an interview prep pack (Telegram + PDF)."""
    from skills.interview_prep import SKILL

    result = SKILL.prep(company=args.company, role=args.role or "", notify=not args.no_send)
    print(result.text)
    if result.data.get("pdf"):
        print(f"\nPDF: {result.data['pdf']}")
    return 0 if result.ok else 1


def cmd_mock(args: argparse.Namespace) -> int:
    """Start an interactive mock interview in the terminal."""
    from skills.interview_prep import SKILL

    res = SKILL.mock(company=args.company)
    print(res.text)
    while not res.data.get("done"):
        try:
            ans = input("\nYour answer (blank to stop)> ").strip()
        except EOFError:
            break
        if not ans:
            break
        res = SKILL.mock_answer(answer=ans)
        print("\n" + res.text)
    return 0


def cmd_hunt(args: argparse.Namespace) -> int:
    """Run one job-hunt pass (scrape -> score -> draft -> digest)."""
    from skills.job_hunter import SKILL

    result = SKILL.hunt(notify=not args.no_send)
    print(result.text)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve drafted jobs by id (email-apply => send; portal/notify => track)."""
    from skills.job_hunter import SKILL

    ids = [int(n) for part in args.ids for n in part.replace(",", " ").split()]
    result = SKILL.approve(selection=ids)
    print(result.text)
    return 0 if result.ok else 1


def cmd_summary(args: argparse.Namespace) -> int:
    """Print (or send) the weekly application report."""
    from skills.job_hunter import SKILL

    result = SKILL.report(notify=not args.no_send)
    print(result.text)
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch a company's careers page for new postings (deep-crawl daily)."""
    from skills.job_hunter import SKILL

    result = SKILL.watch(company=args.company, url=args.url)
    print(result.text)
    return 0 if result.ok else 1


def cmd_auth(_: argparse.Namespace) -> int:
    """Run the Gmail OAuth desktop flow (laptop-only) and write token.json."""
    from core.gmail_client import GmailAuthError, run_oauth_flow

    try:
        path = run_oauth_flow()
    except GmailAuthError as exc:
        log.error("%s", exc)
        return 1
    print(f"Authorized. Token written to {path}.\n"
          f"Copy it to the droplet:  scp {path.name} agentos@<droplet>:~/AgentOS/secrets/\n"
          f"(secrets/, not the project root — that's where the containers read it from.)")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Run one inbox cleanup pass."""
    from skills.email_agent import SKILL

    result = SKILL.cleanup(max_results=args.max)
    print(result.text)
    return 0 if result.ok else 1


def cmd_digest(args: argparse.Namespace) -> int:
    """Build the daily digest; --no-send prints it instead of sending to Telegram."""
    from skills.email_agent import SKILL

    result = SKILL.digest(notify=not args.no_send)
    print(result.text)
    return 0 if result.ok else 1


def cmd_draft(args: argparse.Namespace) -> int:
    """Draft a reply (Gmail DRAFT, never sent) to a message id or the latest actionable email."""
    from skills.email_agent import SKILL

    result = SKILL.draft(instruction=args.instruction, msg_id=args.msg_id or "")
    print(result.text)
    return 0 if result.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the FastAPI kernel with uvicorn."""
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    log.info("Serving AgentOS kernel on %s:%s", host, port)
    uvicorn.run("kernel.app:app", host=host, port=port, reload=args.reload)
    return 0


def _mask_dsn(dsn: str) -> str:
    """Hide the password when printing a connection string."""
    import re

    masked = re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn or "")
    # psycopg also accepts keyword DSNs (``host=... password=...``).  Health output
    # must not leak those credentials merely because the operator chose that format.
    return re.sub(
        r"(?i)(\bpassword\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)",
        r"\1***",
        masked,
    )


def cmd_health(_: argparse.Namespace) -> int:
    """Print a local health snapshot without needing the server running."""
    from kernel.app import registry, scheduler  # imports trigger discovery lazily

    registry.discover()
    from core.memory import get_memory

    settings = get_settings()
    db_ok = True
    try:
        get_memory().conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.error("DB check failed: %s", exc)

    snapshot = {
        "db_ok": db_ok,
        "nim_key_present": bool(settings.nvidia_api_key),
        "skills": sorted(registry.skills.keys()),
        "scheduled_jobs": [j.id for j in registry.all_scheduled_jobs()],
        "scheduler_started": scheduler.running,
        "timezone": settings.tz,
        "database_url": _mask_dsn(settings.database_url),
    }
    print(json.dumps(snapshot, indent=2))
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    """Route a one-off text command through the intent router (offline keyword-only unless --llm)."""
    from kernel.app import registry

    registry.discover()
    intent, result = registry.handle_command(args.text, use_llm=args.llm)
    print(f"intent = {intent.name}  (skill={intent.skill}, action={intent.action}, via={intent.via})")
    if intent.args:
        print(f"args   = {intent.args}")
    print(f"ok     = {result.ok}")
    print(f"reply  = {result.text}")
    return 0 if result.ok else 1


def cmd_backup(args: argparse.Namespace) -> int:
    """Back up BOTH the Postgres database (pg_dump) and the data/ files, into one archive.

    The database no longer lives under data/, so a file-only backup would silently miss all
    durable state (jobs, persona facts, flashcards, deadlines, the flip pipeline...).
    """
    import shutil
    import subprocess
    import tempfile

    settings = get_settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = settings.project_root / f"agentos-backup-{stamp}.tar.gz"

    if shutil.which("pg_dump") is None:
        print("ERROR: pg_dump not found on PATH — cannot back up the database. "
              "Install the postgresql client tools (or pass --files-only to accept a "
              "files-only backup).")
        if not args.files_only:
            return 1

    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "agentos.sql"
        if not args.files_only and shutil.which("pg_dump"):
            proc = subprocess.run(["pg_dump", "--no-owner", "--dbname", settings.database_url,
                                   "--file", str(dump)], capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"ERROR: pg_dump failed: {proc.stderr.strip()[:300]}")
                return 1
        with tarfile.open(out, "w:gz") as tar:
            if dump.exists():
                tar.add(dump, arcname="agentos.sql")     # full DB dump
            if settings.data_dir.exists():
                tar.add(settings.data_dir, arcname="data")  # vault, CVs, PDFs, lectures
    size_mb = out.stat().st_size / 1_000_000
    print(f"Backup written to {out} ({size_mb:.1f} MB)"
          + ("  [files only — NO database]" if args.files_only else "  [database + files]"))
    print("Restore: tar -xzf <archive> && psql -d <DATABASE_URL> -f agentos.sql")
    return 0


def _make_future(name: str, desc: str):
    def _run(_: argparse.Namespace) -> int:
        print(f"'{name}' is not implemented yet — {desc}.")
        return 0

    return _run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage.py", description="AgentOS management CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the FastAPI kernel")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("health", help="print a local health snapshot").set_defaults(func=cmd_health)

    p_cmd = sub.add_parser("command", help="route one text command through the intent router")
    p_cmd.add_argument("text")
    p_cmd.add_argument("--llm", action="store_true", help="allow LLM fallback (needs NVIDIA_API_KEY)")
    p_cmd.set_defaults(func=cmd_command)

    p_bk = sub.add_parser("backup", help="pg_dump the database + tar data/ into one archive")
    p_bk.add_argument("--files-only", action="store_true",
                      help="skip the database dump (files only — NOT a full backup)")
    p_bk.set_defaults(func=cmd_backup)

    # --- Phase 2: email agent ---
    sub.add_parser("auth", help="Gmail OAuth desktop flow (laptop)").set_defaults(func=cmd_auth)

    p_clean = sub.add_parser("cleanup", help="run one inbox cleanup pass")
    p_clean.add_argument("--max", type=int, default=50, help="max messages to scan")
    p_clean.set_defaults(func=cmd_cleanup)

    p_dig = sub.add_parser("digest", help="build/send the daily email digest")
    p_dig.add_argument("--no-send", action="store_true", help="print instead of sending to Telegram")
    p_dig.set_defaults(func=cmd_digest)

    p_draft = sub.add_parser("draft", help="draft a reply (never sends)")
    p_draft.add_argument("instruction", help="what the reply should say")
    p_draft.add_argument("--msg-id", dest="msg_id", default=None, help="target Gmail message id")
    p_draft.set_defaults(func=cmd_draft)

    # --- Phase 3: job hunter ---
    p_hunt = sub.add_parser("hunt", help="run one job-hunt pass")
    p_hunt.add_argument("--no-send", action="store_true", help="print digest instead of Telegram")
    p_hunt.set_defaults(func=cmd_hunt)

    p_appr = sub.add_parser("approve", help="approve drafted jobs by id (e.g. approve 1 3 5)")
    p_appr.add_argument("ids", nargs="+", help="job ids to approve")
    p_appr.set_defaults(func=cmd_approve)

    p_sum = sub.add_parser("summary", help="weekly application report")
    p_sum.add_argument("--no-send", action="store_true", help="print instead of Telegram")
    p_sum.set_defaults(func=cmd_summary)

    p_watch = sub.add_parser("watch", help="watch a company's careers page")
    p_watch.add_argument("company")
    p_watch.add_argument("url")
    p_watch.set_defaults(func=cmd_watch)

    # --- Phase 4: persona ---
    sub.add_parser("persona-init", help="interactive persona seeding interview").set_defaults(
        func=cmd_persona_init)

    # --- Phase 5: form assistant ---
    p_q = sub.add_parser("queue", help="job queue depth, failures, and requeue")
    p_q.add_argument("--requeue", help="requeue failed jobs: a kind name, or 'all'")
    p_q.set_defaults(func=cmd_queue)

    p_pg = sub.add_parser("persona-github", help="import CANDIDATE facts from public GitHub repos")
    p_pg.add_argument("--detailed", action="store_true",
                      help="deterministic deep read: languages, deployed apps, collaborations")
    p_pg.add_argument("user", nargs="?", help="GitHub username (default: $GITHUB_USER)")
    p_pg.add_argument("--no-send", action="store_true", help="don't push a Telegram summary")
    p_pg.set_defaults(func=cmd_persona_github)

    p_pf = sub.add_parser("persona-facts", help="review/confirm candidate persona facts")
    p_pf.add_argument("--verify", help="confirm one, as category.key (e.g. skills.typescript)")
    p_pf.add_argument("--reject", action="store_true", help="with --verify: reject instead")
    p_pf.set_defaults(func=cmd_persona_facts)

    p_form = sub.add_parser("form", help="build an answer sheet from questions (never submits)")
    p_form.add_argument("text", nargs="?", default="", help="pasted questions")
    p_form.add_argument("--url", default=None, help="form URL to fetch")
    p_form.set_defaults(func=cmd_form)

    # --- Phase 6: research & interview prep ---
    p_res = sub.add_parser("research", help="search the web and synthesize a cited answer")
    p_res.add_argument("query")
    p_res.add_argument("--no-send", action="store_true", help="don't push full version to Telegram")
    p_res.set_defaults(func=cmd_research)

    p_prep = sub.add_parser("prep", help="generate an interview prep pack (PDF)")
    p_prep.add_argument("company")
    p_prep.add_argument("--role", default=None)
    p_prep.add_argument("--no-send", action="store_true")
    p_prep.set_defaults(func=cmd_prep)

    p_mock = sub.add_parser("mock", help="interactive mock interview")
    p_mock.add_argument("company")
    p_mock.set_defaults(func=cmd_mock)

    # --- Phase 7: voice ---
    p_voice = sub.add_parser("voice", help="show/list/switch the pre-built TTS voice")
    p_voice.add_argument("alias", nargs="?", default="", help="voice alias to switch to (e.g. zuri)")
    p_voice.set_defaults(func=cmd_voice)

    # --- Phase 8: telegram bot ---
    sub.add_parser("telegram", help="run the Telegram control bot (long-poll)").set_defaults(
        func=cmd_telegram)

    # --- Phase 9: study vault ---
    p_ing = sub.add_parser("ingest", help="ingest course materials into the vault")
    p_ing.add_argument("--unit", default=None, help="only this unit code")
    p_ing.set_defaults(func=cmd_ingest)

    p_ask = sub.add_parser("ask", help="ask the study vault (cites file + page)")
    p_ask.add_argument("question")
    p_ask.add_argument("--unit", default=None)
    p_ask.add_argument("--no-web", action="store_true", help="don't offer a web fallback")
    p_ask.set_defaults(func=cmd_ask)

    p_vault = sub.add_parser("vault", help="study vault status")
    p_vault.set_defaults(func=cmd_vault)

    # --- Phase 21: self-audit / infra recon ---
    p_inf = sub.add_parser("infra", help="self-audit enrolled infra (report-only)")
    p_inf.add_argument("action", nargs="?", default="report",
                       choices=["enroll", "targets", "scan", "report"])
    p_inf.add_argument("target", nargs="?", default=None)
    p_inf.add_argument("--ports", default=None, help="expected open ports, e.g. 80,443")
    p_inf.add_argument("--no-send", action="store_true")
    p_inf.set_defaults(func=cmd_infra)

    # --- Phase 22: music companion ---
    p_mus = sub.add_parser("music", help="Spotify: taste/queue/playlist/discover/dj/transport")
    p_mus.add_argument("action", choices=["connect", "taste", "queue", "playlist", "discover",
                                          "dj", "play", "pause", "next", "previous", "volume",
                                          "devices", "now"])
    p_mus.add_argument("cue", nargs="?", default=None, help="mood/theme/device/volume")
    p_mus.add_argument("--redirect", default="http://127.0.0.1:8888/callback",
                       help="redirect URI registered on your Spotify app (connect only)")
    p_mus.set_defaults(func=cmd_music)

    # --- Phase 20: adaptive behavior layer ---
    p_ad = sub.add_parser("adaptive", help="noticed patterns, proposed rules, skill contracts")
    p_ad.add_argument("action", nargs="?", default="contracts",
                      choices=["candidates", "propose", "confirm", "decline", "retro", "contracts"])
    p_ad.add_argument("id", nargs="?", type=int, default=0, help="signal id")
    p_ad.add_argument("--answer", default=None, help="your weekly retro note")
    p_ad.add_argument("--no-send", action="store_true")
    p_ad.set_defaults(func=cmd_adaptive)

    # --- Phase 16: flash-flip deal broker ---
    p_flip = sub.add_parser("flip", help="flash-flip pipeline (scan/draft/agree/list/buyer/gate/…)")
    p_flip.add_argument("action", choices=["scan", "pipeline", "digest", "expire", "draft",
                                           "agree", "list", "buyer", "gate", "purchase", "delivered",
                                           "inquiry", "stats", "ledger", "report", "reject"])
    p_flip.add_argument("id", nargs="?", type=int, default=0, help="listing id")
    p_flip.add_argument("price", nargs="?", type=float, default=0.0, help="price (agree/list/buyer)")
    p_flip.add_argument("--handle", default=None, help="buyer handle")
    p_flip.add_argument("--platform", default="jiji")
    p_flip.add_argument("--thread", type=int, default=None, help="negotiation thread id")
    p_flip.add_argument("--committed", action="store_true", help="buyer firmly committed")
    p_flip.add_argument("--paid", action="store_true", help="buyer has paid")
    p_flip.add_argument("--available", action="store_true",
                        help="you re-confirmed the item is still available at the agreed price")
    p_flip.add_argument("--approve", action="store_true", help="you approve the purchase")
    p_flip.add_argument("--message", default=None, help="buyer's inbound question (inquiry)")
    p_flip.add_argument("--days", type=int, default=7, help="report window")
    p_flip.add_argument("--views", type=int, default=0)
    p_flip.add_argument("--inquiries", type=int, default=0)
    p_flip.add_argument("--no-send", action="store_true")
    p_flip.set_defaults(func=cmd_flip)

    # --- Phase 15: CV tailoring ---
    sub.add_parser("cv-update", help="ingest the master CV into structured facts").set_defaults(
        func=cmd_cv_update)

    p_cv = sub.add_parser("cv", help="CV status/variants, or tailor to a job description")
    p_cv.add_argument("--tailor", default=None, metavar="JD", help="job description to tailor against")
    p_cv.add_argument("--company", default=None)
    p_cv.add_argument("--facts", action="store_true", help="list parsed CV facts")
    p_cv.set_defaults(func=cmd_cv)

    # --- Phase 14: event scout ---
    p_ev = sub.add_parser("events", help="find free events, or manage interest tags")
    p_ev.add_argument("tag", nargs="?", default=None, help="filter by tag")
    p_ev.add_argument("--tags-action", choices=["list", "add", "remove"], default=None,
                      help="edit interest tags (use with a tag)")
    p_ev.set_defaults(func=cmd_events)

    # --- Phase 13: semester planner ---
    p_brief = sub.add_parser("briefing", help="unified morning briefing")
    p_brief.add_argument("--no-send", action="store_true")
    p_brief.set_defaults(func=cmd_briefing)

    p_plan = sub.add_parser("plan", help="propose the week plan")
    p_plan.add_argument("--no-send", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_cram = sub.add_parser("cram", help="panic mode: revision + mock CAT for a unit")
    p_cram.add_argument("unit")
    p_cram.add_argument("--days", type=int, default=5)
    p_cram.add_argument("--no-send", action="store_true")
    p_cram.set_defaults(func=cmd_cram)

    p_dl = sub.add_parser("deadline", help="add a deadline, or list what's due")
    p_dl.add_argument("title", nargs="?", default="")
    p_dl.add_argument("due", nargs="?", default="", help="YYYY-MM-DD")
    p_dl.add_argument("--unit", default=None)
    p_dl.add_argument("--type", default=None, help="CAT|assignment|exam|lab")
    p_dl.add_argument("--weight", type=float, default=1.0)
    p_dl.add_argument("--days", type=int, default=7, help="window for listing")
    p_dl.set_defaults(func=cmd_deadline)

    # --- Phase 12: code tutor ---
    p_rev = sub.add_parser("review", help="tutor-style review of a code file")
    p_rev.add_argument("file")
    p_rev.add_argument("--unit", default=None)
    p_rev.add_argument("--rewrite", action="store_true", help="allow a full corrected version")
    p_rev.set_defaults(func=cmd_review)

    p_tut = sub.add_parser("tutor", help="tutor mode: 'explain X' | 'drill X' | 'socratic X' | 'mock lab X'")
    p_tut.add_argument("topic", help="e.g. 'drill linked lists'")
    p_tut.set_defaults(func=cmd_tutor)

    # --- Phase 11: spaced repetition ---
    p_quiz = sub.add_parser("quiz", help="interactive flashcard review")
    p_quiz.add_argument("--unit", default=None)
    p_quiz.set_defaults(func=cmd_quiz)

    p_cards = sub.add_parser("cards", help="list/approve/reject candidate flashcards")
    p_cards.add_argument("--unit", default=None)
    p_cards.add_argument("--approve", type=int, default=None, metavar="ID")
    p_cards.add_argument("--reject", type=int, default=None, metavar="ID")
    p_cards.set_defaults(func=cmd_cards)

    p_rr = sub.add_parser("review-report", help="weekly spaced-repetition report")
    p_rr.add_argument("--no-send", action="store_true")
    p_rr.set_defaults(func=cmd_review_report)

    # --- Phase 10: lecture capture ---
    p_lec = sub.add_parser("lecture", help="process lecture audio (path) or the whole inbox")
    p_lec.add_argument("path", nargs="?", default="", help="audio file (omit to process inbox)")
    p_lec.add_argument("--unit", default=None)
    p_lec.add_argument("--no-send", action="store_true")
    p_lec.set_defaults(func=cmd_lecture)

    for name, desc in _FUTURE.items():
        sub.add_parser(name, help=f"[stub] {desc}").set_defaults(func=_make_future(name, desc))

    return parser


def main(argv: list[str] | None = None) -> int:
    # Ensure emoji/UTF-8 output works on Windows' legacy cp1252 console (no-op on Linux).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
