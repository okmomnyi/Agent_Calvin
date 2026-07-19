// PM2 process definitions for the AgentOS droplet.
// The API (kernel + APScheduler), the queue worker, and the Telegram bot run as independent,
// independently restartable processes — if one dies the others keep working. The laptop voice
// client is NOT managed here (it runs on Calvin's laptop; see client/autostart/).
//
//   pm2 start ecosystem.config.js
//   pm2 restart agentos-api        # after: git pull
//   pm2 logs agentos-bot
//   pm2 save && pm2 startup        # persist across reboots
//
// agentos-worker is NOT optional. Since Phase 26 the scheduler ENQUEUES the heavy jobs
// (job_hunter.hunt, vault.ingest, lecture.inbox, flip.scan, events.scan, proactive.triage)
// instead of running them; `queued=True` means the API process writes a job_queue row and
// returns. With no worker those rows are never claimed, so the hunt silently stops finding
// jobs and the 05:30 triage never runs — with nothing in the logs to say so, because
// enqueueing succeeded. This file defined only api+bot for the whole of Phase 26, so the
// documented PM2 deployment could not drain its own queue.
//
// Scale it the way compose does with `--scale worker=3`: claims use FOR UPDATE SKIP LOCKED,
// so N workers take N different rows.
//   pm2 scale agentos-worker 3

const PY = "./.venv/bin/python"; // adjust if your venv path differs

module.exports = {
  apps: [
    {
      name: "agentos-api",
      script: PY,
      args: "-m uvicorn kernel.app:app --host 0.0.0.0 --port 8000",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      env: { PYTHONUNBUFFERED: "1" },
      out_file: "logs/api.out.log",
      error_file: "logs/api.err.log",
    },
    {
      name: "agentos-worker",
      script: PY,
      args: "-m kernel.worker",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      // SIGTERM lets the in-flight job finish before exit, so a restart loses no work.
      kill_timeout: 30000,
      env: { PYTHONUNBUFFERED: "1" },
      out_file: "logs/worker.out.log",
      error_file: "logs/worker.err.log",
    },
    {
      name: "agentos-bot",
      script: PY,
      args: "manage.py telegram",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 20,
      env: { PYTHONUNBUFFERED: "1" },
      out_file: "logs/bot.out.log",
      error_file: "logs/bot.err.log",
    },
  ],
};
