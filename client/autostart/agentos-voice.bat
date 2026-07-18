@echo off
REM AgentOS voice client autostart (Windows).
REM Compatibility launcher. The PowerShell supervisor resolves this checkout's real path,
REM reads the token from .env, maintains the SSH tunnel, and uses .python\python.exe.
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0agentos-laptop.ps1"
