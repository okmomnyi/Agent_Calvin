# AgentOS Voice Client (laptop)

Always-on voice control for AgentOS. Wake word → speak → hear the reply. Runs on your
laptop and talks to the droplet over an authed WebSocket. CPU-only; no GPU needed.

> **Voices are pre-built only.** This client only ever plays the stock Microsoft edge-tts
> neural voice the server reports (Guy/Aria/Ryan/Rafiki/Zuri). There is no path to record,
> train, or clone a voice — that is permanently out of scope (§0 Principle 9).

## Install

```bash
cd AgentOS
python -m venv .venv
# Windows:  .venv\Scripts\activate     macOS/Linux:  source .venv/bin/activate
pip install -r client/requirements.txt
```

Set the connection env vars (a shell profile, or the autostart file):

```bash
export AGENT_WS_URL="wss://agent.example.com/ws/voice"   # your droplet
export AGENT_WS_TOKEN="<same AGENT_WS_TOKEN as the droplet .env>"
# optional:
export AGENT_WAKE_WORD="hey_jarvis"     # an openwakeword built-in model name
export AGENT_WHISPER_MODEL="small"      # tiny|base|small (small recommended)
export AGENT_PTT_KEY="ctrl+space"       # push-to-talk key
```

Mic permission: macOS will prompt on first run (System Settings → Privacy → Microphone).
On Linux, `keyboard` (push-to-talk) may need `sudo`; wake-word mode does not.

## Run

```bash
python client/voice_client.py          # wake-word mode ("Hey Agent, …")
python client/voice_client.py --ptt    # push-to-talk (hold the key) — good for noisy rooms
```

Flow: wake word → chime → speak (records until ~1.2s silence) → local faster-whisper
transcribes → sent to the droplet → spoken reply. Say **"stop"/"cancel"** to abort locally;
saying the wake word during playback (**barge-in**) cuts the speech and listens again.

Things to say: "check my email", "any new jobs?", "any free events?", "search for X",
"prep me for <company>", "mock interview for <company>", "change voice to Zuri",
"speak slower", "remember that …", "stop".

## Autostart on boot/login

Files in `client/autostart/` — edit the paths + `AGENT_WS_TOKEN` in each first:

- **Linux:** `agentos-voice.service` → `~/.config/systemd/user/`, then
  `systemctl --user enable --now agentos-voice`
- **macOS:** `com.example.agentos.voice.plist` → `~/Library/LaunchAgents/`, then
  `launchctl load ~/Library/LaunchAgents/com.example.agentos.voice.plist`
- **Windows:** drop a shortcut to `agentos-voice.bat` into the `shell:startup` folder

## Phone access (no app to install)

Two paths reuse this exact backend — nothing behaves differently on the phone:

1. **Telegram voice notes** (Phase 8) — send a voice note to the bot; it's transcribed on
   the droplet and routed through the same intent engine, replying as text (+ optional audio).
2. **Push-to-talk shortcut** — an iOS Shortcut / Android tap that records and POSTs to
   `POST https://<droplet>/api/command` with `{"text": "<your words>"}` and the header
   `Authorization: Bearer <AGENT_WS_TOKEN>`. Same brain, just explicitly triggered instead
   of wake-word triggered.

## Troubleshooting

- **No audio device / `PortAudioError`:** check the OS default input/output; on Linux install `libportaudio2`.
- **Wake word never fires:** try `--ptt`, or set a different `AGENT_WAKE_WORD` built-in model.
- **`Unauthorized` from the server:** `AGENT_WS_TOKEN` must match the droplet's `.env`.
- **Slow transcription:** use `AGENT_WHISPER_MODEL=base` or `tiny`.
