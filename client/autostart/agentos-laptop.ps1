# AgentOS laptop client - autostart supervisor (Windows).
#
# NOTE: this file is deliberately pure ASCII. Windows PowerShell 5.1 reads a .ps1 as ANSI
# (cp1252) unless it has a UTF-8 BOM, so a stray em-dash or arrow becomes mojibake that
# breaks string quoting and the whole script fails to parse. Keep it ASCII.
#
# Starts at login and keeps two things alive for as long as you are logged in:
#   1. an SSH tunnel  laptop:8000 -> droplet:8000
#   2. the voice client, talking to the droplet through it
#
# Why a supervisor rather than just launching the client:
#
#   * The droplet's API binds to 127.0.0.1 ONLY. Nothing is exposed to the internet (a bare
#     0.0.0.0 bind is exactly what Phase 21's recon scan flags), so the tunnel is the way in.
#   * BOTH die routinely, and neither is an error worth bothering you about. Closing the lid
#     kills the tunnel; so does a mobile-network blip - one dropped with "Connection reset by
#     peer" while this was being written. A one-shot launcher is dead after your first sleep,
#     and you would not find out until you asked it something and got silence.
#
# So: relaunch forever with a short backoff, so an offline laptop does not spin.
# Opening the lid re-establishes everything within ~5 seconds, which is the point.
#
# Install:  powershell -ExecutionPolicy Bypass -File client\autostart\install-autostart.ps1
# Logs:     %LOCALAPPDATA%\AgentOS\laptop.log
# Stop:     Task Manager -> end the powershell/python processes, or log out.

$ErrorActionPreference = "Continue"

# ---- single instance ---------------------------------------------------------
# Without this, a second supervisor (login + a manual start, or an install re-run) launches a
# SECOND voice client: two processes fighting over one microphone, two whisper models at
# ~570MB each, and two replies to every wake word. The tunnel self-limits via
# ExitOnForwardFailure, but the clients happily stack up. Observed: 3 supervisors at once.
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
# The token is READ FROM .env, never copied in here: one source of truth, and no secret
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

$DropletIp = if ($env:AGENT_DROPLET_IP) { $env:AGENT_DROPLET_IP } else { "167.172.106.161" }
$SshKey    = if ($env:AGENT_SSH_KEY)    { $env:AGENT_SSH_KEY }    else { Join-Path $env:USERPROFILE ".ssh\Pay-to-Connect" }
$LocalPort = if ($env:AGENT_LOCAL_PORT) { $env:AGENT_LOCAL_PORT } else { "8000" }
# The window (Phase 24), not the old wake-word daemon. It starts in the tray with the mic
# CLOSED and opens it only when Calvin clicks. The wake word is still there behind
# AGENT_CLIENT_MODE=voice for anyone who wants hands-free, but it is no longer the default:
# an always-on microphone is not something to opt out of, it is something to opt in to.
$Mode      = if ($env:AGENT_CLIENT_MODE){ $env:AGENT_CLIENT_MODE }else { "window" }
$WsToken   = Get-EnvValue "AGENT_WS_TOKEN"

if (-not $WsToken) { Log "FATAL: AGENT_WS_TOKEN not found in $EnvFile"; exit 1 }
if (-not (Test-Path $SshKey)) { Log "FATAL: ssh key not found: $SshKey"; exit 1 }

# Pick an interpreter that can actually import tkinter. The window (Phase 24) needs it, and the
# bundled embeddable python (.python\python.exe) does NOT ship tkinter -- so preferring it made
# the window crash-loop on "No module named 'tkinter'" while the supervisor kept relaunching it.
# The client deps (tkinter, pystray, sounddevice, whisper) all live in the full Python install
# on PATH, so verify each candidate rather than trusting a path.
function Test-PyTk($py) {
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
foreach ($c in $candidates) { if (Test-PyTk $c) { $Python = $c; break } }
if (-not $Python) {
    # Window can't run without tkinter, but voice/--text modes can -- fall back so those work.
    $Python = if (Test-Path $BundledPython) { $BundledPython }
              else { (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { Log "FATAL: no Python found at all"; exit 1 }
    Log "WARNING: no python with tkinter; window mode will fail. Using $Python"
} else {
    Log "python: $Python (tkinter OK)"
}

$env:AGENT_WS_URL   = "ws://localhost:$LocalPort/ws/voice"
$env:AGENT_WS_TOKEN = $WsToken

$modeName = if ($Mode) { $Mode } else { "wake-word" }
Log "starting - droplet $DropletIp, mode '$modeName'"

# ---- tunnel supervisor (background job) --------------------------------------
$tunnel = Start-Job -Name "agentos-tunnel" -ScriptBlock {
    param($ip, $key, $port, $logFile)
    while ($true) {
        # ExitOnForwardFailure: fail fast if the port is already bound, instead of sitting
        #   there with a tunnel that forwards nothing.
        # ServerAliveInterval: notice a silently dead link (sleep) instead of hanging.
        & ssh -N -L "${port}:localhost:8000" -i $key `
            -o BatchMode=yes -o ExitOnForwardFailure=yes `
            -o ServerAliveInterval=20 -o ServerAliveCountMax=3 `
            -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 `
            "root@$ip" 2>&1 | Out-Null
        $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $logFile -Encoding utf8 -Value "$stamp  tunnel dropped (sleep or network), reconnecting in 5s"
        Start-Sleep -Seconds 5
    }
} -ArgumentList $DropletIp, $SshKey, $LocalPort, $LogFile

# ---- client supervisor (foreground) ------------------------------------------
try {
    while ($true) {
        # Do not start the client until the tunnel actually answers, or it just errors on
        # connect and we churn.
        $ready = $false
        for ($i = 0; $i -lt 12; $i++) {
            try {
                Invoke-WebRequest -Uri "http://localhost:$LocalPort/api/health" -TimeoutSec 3 -UseBasicParsing | Out-Null
                $ready = $true
                break
            } catch { Start-Sleep -Seconds 5 }
        }
        if (-not $ready) { Log "tunnel not up after 60s, retrying"; continue }

        Log "tunnel up, launching client ($modeName)"
        Push-Location $ClientDir
        # Capture the client's own output. An earlier version just ran python and logged
        # "voice client exited", which is useless: it crash-looped for minutes on a missing
        # tflite runtime while the log said only that it exited, the reason going to a hidden
        # console nobody could read. Whatever kills it must land in this file, or the restart
        # loop hides the bug forever.
        #
        # -u because a redirected python block-buffers and you get nothing until it dies.
        # Add-Content -Encoding utf8 rather than Tee-Object: Tee writes UTF-16 in PS 5.1, and
        # mixing that into a UTF-8 log makes the file half NUL bytes ("binary file matches").
        if ($Mode -eq "window") { $out = & $Python -u agent_window.py --tray 2>&1 }
        elseif ($Mode -eq "voice") { $out = & $Python -u voice_client.py 2>&1 }  # opt-in wake word
        else { $out = & $Python -u voice_client.py $Mode 2>&1 }                  # --text / --ptt
        $out | ForEach-Object { Add-Content -Path $LogFile -Value ("    " + $_) -Encoding utf8 }
        Pop-Location
        Log "voice client exited (see output above), restarting in 5s"
        Start-Sleep -Seconds 5
    }
} finally {
    Stop-Job $tunnel -ErrorAction SilentlyContinue
    Remove-Job $tunnel -Force -ErrorAction SilentlyContinue
    Log "supervisor stopped"
}
