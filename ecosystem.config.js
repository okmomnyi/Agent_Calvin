// PM2 process definitions for the AgentOS droplet.
// The API (kernel + APScheduler) and the Telegram bot run as independent, independently
// restartable processes — if one dies the other keeps working. The laptop voice client
// is NOT managed here (it runs on Calvin's laptop; see client/autostart/).
//
//   pm2 start ecosystem.config.js
//   pm2 restart agentos-api        # after: git pull
//   pm2 logs agentos-bot
//   pm2 save && pm2 startup        # persist across reboots

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
