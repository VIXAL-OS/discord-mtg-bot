# Bot launcher -- the one sanctioned way to start the bot detached.
#
# Exists because detached launches have three documented traps (CLAUDE.md,
# Aug 11 / Aug 23 2026):
#   1. PowerShell's -RedirectStandardOutput hands Python a handle in the
#      system ANSI codepage, and the startup banner prints emoji -- without
#      PYTHONIOENCODING=utf-8 the process dies on a UnicodeEncodeError
#      BEFORE reaching the gateway, leaving an empty stdout log.
#   2. Python block-buffers a redirected stdout, so the log stays empty for
#      minutes while the bot is healthy -- PYTHONUNBUFFERED=1, and judge a
#      launch by stderr's gateway line, not stdout.
#   3. A second instance double-connects the Discord token and every message
#      gets two replies. The guard below refuses to start when a bot.py
#      under this repo's venv is already running (same-user visibility only,
#      which matches how these launches happen).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\launch_bot.ps1
#   ... -Strict                                  # MTG_STRICT=1 (audit batches)
#   ... -Autoplay "!autoplay-parallel all 25"    # fire a batch at login
#
# The XMage bridge needs nothing here -- the MTG cog spawns it in cog_load.
# Plain boot launches deliberately do NOT set MTG_STRICT: live games stay
# crash-proof; strict is for batches (the standing convention).

param(
    [switch]$Strict,
    [string]$Autoplay = ""
)

$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path $repo "venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Output "REFUSED: no venv python at $py"
    exit 1
}

# Single-instance guard: any same-user python running bot.py under THIS
# repo's venv (the shim parent/child pair both match -- either is enough).
$pattern = [regex]::Escape($py)
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match $pattern -and $_.CommandLine -match "bot\.py" }
if ($running) {
    $pids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Output "REFUSED: bot already running (pid $pids) -- a second instance would double-connect the token"
    exit 2
}

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"
if ($Strict) { $env:MTG_STRICT = "1" } else { Remove-Item Env:\MTG_STRICT -ErrorAction SilentlyContinue }
if ($Autoplay) { $env:MTG_AUTOSTART_COMMAND = $Autoplay } else { Remove-Item Env:\MTG_AUTOSTART_COMMAND -ErrorAction SilentlyContinue }

$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$logdir = Join-Path $repo "logs"
if (-not (Test-Path $logdir)) { New-Item -ItemType Directory -Path $logdir | Out-Null }
$out = Join-Path $logdir "boot_${ts}_stdout.log"
$err = Join-Path $logdir "boot_${ts}_stderr.log"

Start-Process -FilePath $py -ArgumentList "bot.py" -WorkingDirectory $repo `
    -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden

Write-Output "launched: logs/boot_${ts}_*.log (strict=$([bool]$Strict), autoplay='$Autoplay')"
exit 0
