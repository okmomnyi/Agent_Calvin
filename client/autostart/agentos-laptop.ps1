# AgentOS laptop client - autostart supervisor (Windows).
#
# NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads a .ps1 as ANSI
# (cp1252) unless it has a UTF-8 BOM, so a stray em-dash or arrow becomes mojibake that
# breaks string quoting and the whole script fails to parse. Keep it ASCII.
#
# Starts at login and keeps the client alive for as long as you are logged in.
#
# Phase 36: no SSH tunnel anymore. The old version forwarded laptop:8000 -> droplet:8000
# because the kernel bound 127.0.0.1-only with nothing exposed publicly. That is still true
# of the kernel itself, but it is no longer the whole story: Caddy now terminates real TLS on
# AGENT_WS_URL's own domain (see .env) and reverse-proxies /api/* and /ws/voice with real
# session/device-credential auth in front of it -- exactly the path .env's AGENT_WS_URL
# already points at. Tunnelling to a bare, unauthenticated localhost:8000 instead of using
# that was redundant at best and a second, weaker way in at worst. Every client (window mode
# AND voice_client.py) just uses AGENT_WS_URL from .env directly now.
#
# Also Phase 36: launches hud_window.py (the pywebview HUD, one codebase with the web
# dashboard) instead of the old agent_window.py (Tkinter). agent_window.py still exists and
# still works standalone if you want it, but it is not what this supervisor starts anymore.
#
# Why a supervisor rather than just launching the client:
#
#   * It dies routinely, and that is not an error worth bothering you about. Closing the lid,
#     a mobile-network blip, a droplet restart -- none of those should mean "manually notice
#     and relaunch it."
#
# So: relaunch forever with a short backoff, so an offline laptop does not spin.
#
# Install:  powershell -ExecutionPolicy Bypass -File client\autostart\install-autostart.ps1
# Logs:     %LOCALAPPDATA%\AgentOS\laptop.log
# Stop:     Task Manager -> end the powershell/python processes, or log out.

$ErrorActionPreference = "Continue"

# ---- single instance ---------------------------------------------------------
# Without this, a second supervisor (login + a manual start, or an install re-run) launches a
# SECOND client fighting the first one for the microphone. Observed: 3 supervisors at once.
$mutex = New-Object System.Threading.Mutex($false, "Global\AgentOSLaptopSupervisor")
if (-not $mutex.WaitOne(0)) {
    Write-Host "AgentOS supervisor already running - exiting."
    exit 0
}

# ---- paths -------------------------------------------------------------------
$Root      = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # ...\Agentic OS
$ClientDir = Join-Path $Root "client"
$EnvFile   = Join-Path $Root ".env"
$BundledPython = Join-Path $Root ".python\python.exe"
$LogDir    = Join-Path $env:LOCALAPPDATA "AgentOS"
$LogFile   = Join-Path $LogDir "laptop.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

# ---- config ------------------------------------------------------------------
# Tokens/URLs are READ FROM .env, never copied in here: one source of truth, and no secret
# sitting in a file that lives in the Startup folder. The regex strips an inline comment,
# because "TOKEN=abc   # note" is otherwise read as the value "abc   # note".
function Get-EnvValue($name) {
    if (-not (Test-Path $EnvFile)) { return "" }
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match "^\s*$name\s*=\s*(.*)$") {
            return ($matches[1] -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

# The window (Phase 24, now the pywebview HUD as of Phase 36), not the old wake-word daemon.
# It starts in the tray with the mic CLOSED and opens it only when Calvin clicks. The wake
# word is still there behind AGENT_CLIENT_MODE=voice for anyone who wants hands-free, but it
# is no longer the default: an always-on microphone is not something to opt out of, it is
# something to opt in to.
$Mode        = if ($env:AGENT_CLIENT_MODE){ $env:AGENT_CLIENT_MODE }else { "window" }
# Slice 0e: `manage.py login issue-device-token` on the droplet, pasted into .env as
# AGENT_DEVICE_TOKEN — replaces the old shared AGENT_WS_TOKEN, which no code path reads.
$DeviceToken = Get-EnvValue "AGENT_DEVICE_TOKEN"
$WsUrl       = Get-EnvValue "AGENT_WS_URL"

if (-not $DeviceToken) { Log "FATAL: AGENT_DEVICE_TOKEN not found in $EnvFile"; exit 1 }
if (-not $WsUrl) { Log "FATAL: AGENT_WS_URL not found in $EnvFile"; exit 1 }

# hud_window.py needs pywebview/pynput/screeninfo (not tkinter) but the SAME "must be the
# full interpreter, not the dependency-free bundled one" reasoning applies -- the bundled
# embeddable python (.python\python.exe) ships none of the client's optional deps, so
# preferring it makes the window crash-loop on a missing import. Verify each candidate
# rather than trusting a path; tkinter is just a convenient, cheap probe for "is this the
# real install with pip-installed extras, not the bundled minimal one."
function Test-FullPython($py) {
    if (-not $py -or -not (Test-Path $py)) { return $false }
    & $py -c "import tkinter" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}
$candidates = @(
    (Get-Command python -ErrorAction SilentlyContinue).Source,
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    $BundledPython
) | Where-Object { $_ } | Select-Object -Unique
$Python = $null
foreach ($c in $candidates) { if (Test-FullPython $c) { $Python = $c; break } }
if (-not $Python) {
    $Python = if (Test-Path $BundledPython) { $BundledPython }
              else { (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { Log "FATAL: no Python found at all"; exit 1 }
    Log "WARNING: no full python install found; client deps may be missing. Using $Python"
} else {
    Log "python: $Python"
}

$env:AGENT_WS_URL = $WsUrl
$env:AGENT_DEVICE_TOKEN = $DeviceToken

# derive an https base for the readiness check from the SAME URL every client authenticates
# against, so this never probes a different server than the one about to be used.
$HealthUrl = ($WsUrl -replace '^wss://', 'https://' -replace '^ws://', 'http://') -replace '/ws/voice.*$', '/api/health'

$modeName = if ($Mode) { $Mode } else { "wake-word" }
Log "starting - server $WsUrl, mode '$modeName'"

# A client that dies within seconds of launching (a WebView2 profile race, a bad network
# blip right as Wi-Fi reconnects) used to get relaunched every flat 5s, which reads as "the
# window keeps popping up and stealing focus" rather than as a backoff. Doubles up to 60s on
# a fast exit, resets to 5s the moment a launch survives a real session.
$RestartDelay = 5
$MinHealthyRunSeconds = 20

# ---- client supervisor (foreground) ------------------------------------------
try {
    while ($true) {
        # Do not start the client before the server actually answers, or it just errors on
        # connect and we churn -- matters right after boot/login before Wi-Fi is up.
        $ready = $false
        for ($i = 0; $i -lt 12; $i++) {
            try {
                Invoke-WebRequest -Uri $HealthUrl -TimeoutSec 5 -UseBasicParsing | Out-Null
                $ready = $true
                break
            } catch { Start-Sleep -Seconds 5 }
        }
        if (-not $ready) { Log "server not reachable after 60s, retrying"; continue }

        Log "server up, launching client ($modeName)"
        Push-Location $ClientDir
        # Capture the client's own output. An earlier version just ran python and logged
        # "voice client exited", which is useless: it crash-looped for minutes on a missing
        # dependency while the log said only that it exited, the reason going to a hidden
        # console nobody could read. Whatever kills it must land in this file, or the restart
        # loop hides the bug forever.
        #
        # -u because a redirected python block-buffers and you get nothing until it dies.
        # Add-Content -Encoding utf8 rather than Tee-Object: Tee writes UTF-16 in PS 5.1, and
        # mixing that into a UTF-8 log makes the file half NUL bytes ("binary file matches").
        $launchedAt = Get-Date
        if ($Mode -eq "window") { $out = & $Python -u hud_window.py --tray 2>&1 }
        elseif ($Mode -eq "voice") { $out = & $Python -u voice_client.py 2>&1 }  # opt-in wake word
        else { $out = & $Python -u voice_client.py $Mode 2>&1 }                  # --text / --ptt
        $exitCode = $LASTEXITCODE
        $out | ForEach-Object { Add-Content -Path $LogFile -Value ("    " + $_) -Encoding utf8 }
        Pop-Location

        $ranForSeconds = ((Get-Date) - $launchedAt).TotalSeconds

        # hud_window.py has no OS close box -- the tray's own "Quit" (or the page's quit()
        # bridge call) is the ONLY way to close it, and both return cleanly (exit 0). Without
        # this check, quitting looked identical to a crash: the window came right back a few
        # seconds later, forever, because nothing here ever distinguished "the user asked to
        # quit" from "it died unexpectedly". Scoped to window mode only -- voice_client.py's
        # own loops can plausibly return 0 after a recoverable network blip (not just
        # Ctrl+C), and this supervisor exists specifically so THAT case keeps retrying rather
        # than going silently, permanently dark.
        if ($Mode -eq "window" -and $exitCode -eq 0) {
            Log ("client exited cleanly after {0:N0}s (exit 0) -- treating as an intentional " +
                "quit, supervisor stopping. Re-run this script (or log back in) to bring it back.")
            break
        }

        if ($ranForSeconds -lt $MinHealthyRunSeconds) {
            $RestartDelay = [Math]::Min($RestartDelay * 2, 60)
            Log ("client exited after {0:N0}s (exit {1}) -- crash-loop backoff, waiting {2}s" -f $ranForSeconds, $exitCode, $RestartDelay)
        } else {
            $RestartDelay = 5
            Log ("client exited after {0:N0}s (exit {1}), restarting in {2}s" -f $ranForSeconds, $exitCode, $RestartDelay)
        }
        Start-Sleep -Seconds $RestartDelay
    }
} finally {
    Log "supervisor stopped"
}
