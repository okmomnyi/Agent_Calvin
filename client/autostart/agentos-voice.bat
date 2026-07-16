@echo off
REM AgentOS voice client autostart (Windows).
REM Install: press Win+R, type  shell:startup , press Enter, and drop a SHORTCUT to this
REM .bat in that folder. Edit the paths/token below first.

set AGENT_WS_URL=wss://agent.example.com/ws/voice
set AGENT_WS_TOKEN=CHANGE_ME

cd /d "%USERPROFILE%\AgentOS\client"
"%USERPROFILE%\AgentOS\.venv\Scripts\python.exe" voice_client.py
