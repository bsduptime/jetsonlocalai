# ============================================================================
# hermes-household-board — Windows setup (Task Scheduler + config skeleton)
# ============================================================================
# Windows counterpart of install-household-board.sh (same pattern: config
# skeleton, fail-closed allowlist, service survives reboot). Run from the
# repo root in an ELEVATED PowerShell:
#   powershell -ExecutionPolicy Bypass -File hermes-agent\household-board\setup-household-board.ps1
#
# Division of labor on a Windows box (mirrors the calendar relay):
#   - Hermes (+ household plugin)  runs as the sandboxed 'hermes' user
#   - the board                    runs as the admin 'user' account via a
#                                  Task Scheduler job at startup
#   - both share HOUSEHOLD_STATE_DIR (machine env var + ACL for 'hermes')
# ============================================================================

$ErrorActionPreference = "Stop"

$RepoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # ...\jetsonlocalai
$BoardPy    = Join-Path $PSScriptRoot "board.py"
$ConfDir    = "C:\ProgramData\hermes-household-board"
$StateDir   = "C:\ProgramData\hermes-household"
$HermesPy   = "C:\Users\hermes\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
$TaskName   = "hermes-household-board"

if (-not (Test-Path $BoardPy))  { throw "board.py not found at $BoardPy" }
if (-not (Test-Path $HermesPy)) { throw "Hermes bundled python not found at $HermesPy — install Hermes first" }

# --- shared state dir: plugin (hermes) + board (user) both read/write -------
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
icacls $StateDir /grant "hermes:(OI)(CI)M" | Out-Null
# Plugin + board resolve the same dir via the machine-level env var.
[Environment]::SetEnvironmentVariable("HOUSEHOLD_STATE_DIR", $StateDir, "Machine")

# --- config: token + allowlist (fail closed / discovery mode) ---------------
New-Item -ItemType Directory -Force -Path $ConfDir | Out-Null
$EnvFile = Join-Path $ConfDir "board.env"
if (-not (Test-Path $EnvFile)) {
@"
# hermes-household-board — Windows config (KEY=VALUE lines)
# Token for the DEDICATED board bot from @BotFather (NOT the agent's token).
# Privacy mode stays ON — the board only ever receives /commands and taps.
HOUSEHOLD_BOT_TOKEN=
# Comma-separated chat ids. Leave empty to start in DISCOVERY MODE: the board
# serves no chat but logs the id of every /list attempt (see board.log).
HOUSEHOLD_ALLOWED_CHAT_IDS=
"@ | Set-Content -Encoding UTF8 $EnvFile
    Write-Host "created $EnvFile - EDIT IT (token; allowlist can wait for discovery)"
} else {
    Write-Host "$EnvFile exists - leaving it alone"
}
# Only admins + hermes can read the token file.
icacls $ConfDir /inheritance:d | Out-Null
icacls $ConfDir /remove "Users" 2>$null | Out-Null
icacls $ConfDir /grant "hermes:(OI)(CI)R" | Out-Null

# --- runner: load board.env into env, exec board.py, log to file ------------
$Runner = Join-Path $ConfDir "run-board.ps1"
@"
`$ErrorActionPreference = 'Continue'
Get-Content '$EnvFile' | ForEach-Object {
    if (`$_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
        [Environment]::SetEnvironmentVariable(`$Matches[1], `$Matches[2].Trim())
    }
}
[Environment]::SetEnvironmentVariable('HOUSEHOLD_STATE_DIR', '$StateDir')
& '$HermesPy' '$BoardPy' *>> '$ConfDir\board.log'
"@ | Set-Content -Encoding UTF8 $Runner

# --- Task Scheduler: at startup, restart on failure --------------------------
$Action   = New-ScheduledTaskAction -Execute "powershell.exe" `
            -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`""
$Trigger  = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Days 3650) -StartWhenAvailable
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -RunLevel Limited | Out-Null

Write-Host ""
Write-Host "Installed. Next:"
Write-Host "  1. @BotFather -> /newbot ('Family List') -> token into $EnvFile"
Write-Host "  2. Start-ScheduledTask -TaskName $TaskName"
Write-Host "  3. Send /list in the target chat; read the chat id from $ConfDir\board.log"
Write-Host "  4. Put the id into HOUSEHOLD_ALLOWED_CHAT_IDS, then:"
Write-Host "     Stop-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName"
Write-Host "  5. /list again -> tappable board."
