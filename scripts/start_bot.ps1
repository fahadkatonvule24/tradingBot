# Opens MetaTrader 5, the live trading loop and the dashboard, each only if it is not running.
#
# Run by the "tradebot autostart" scheduled task at logon and every 5 minutes after, so a reboot
# (Windows Update, a power cut, a shutdown) or a stopped loop or dashboard (Ctrl+C, a closed
# window, a crash) leaves it off for at most 5 minutes. Safe to run by hand at any time: it never
# starts a second terminal, loop or dashboard (the loop also refuses a second copy in the folder).
#
# A frozen loop is restarted too. The loop rewrites tradebot.loop.heartbeat after every cycle
# (about every 15 seconds); a loop whose heartbeat is older than -FrozenMinutes (counted from
# the loop's start if it started later) is stuck, usually in a MetaTrader 5 call that never
# returned after the terminal reconnected (23 Nov 2026: 2 h 18 min with no cycle). Its processes
# are stopped (only the loop's: MT5 stays open) and a new loop is started, which sends the
# reason, left in tradebot.loop.frozen, to the phone.
#
# The dashboard (tradebot ui --live) opens in the browser when this script has just opened MT5,
# as after a reboot. When it only brings back a dashboard that stopped, no browser tab is opened:
# the page is at http://127.0.0.1:8765/ (or the next free port if that one is taken).
#
# MT5 is opened here, not by the bot: a terminal the bot's own process starts is that process's
# child, and stopping the bot would then close the terminal with it.
#
# No window pops up. The loop and the dashboard start minimized (on the taskbar), each in a classic
# console window with QuickEdit turned off by the bot, so a click in it cannot pause it. The task
# runs this script through a headless console: "powershell -WindowStyle Hidden" alone still
# flashed a black window every 5 minutes. The task's action:
#
#   C:\Windows\System32\conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass
#       -WindowStyle Hidden -File "E:\GIT REPOS\tradingBot\scripts\start_bot.ps1"
#
# By hand (a window is fine then):
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_bot.ps1
#
# Files in the tradingBot folder that change what it does:
#   STOP           the loop keeps running and managing open trades, but opens no new ones
#   OFF            this script starts nothing (MT5, loop or dashboard) until the file is deleted
#   DASHBOARD_OFF  the dashboard is not started; MT5 and the loop are
# Only actions are logged, to start_bot.log.

param(
    [string]$Terminal = "C:\Program Files\MetaTrader 5\terminal64.exe",
    [string]$Config = "config/live.local.json",
    [int]$ConnectSeconds = 45,
    [int]$FrozenMinutes = 10
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "start_bot.log"
$exe = Join-Path $root ".venv\Scripts\tradebot.exe"

function Write-Line([string]$text) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $text
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

function Find-Tradebot([string]$command) {
    # however it was started: tradebot.exe, or python -m tradebot (as another tool may start it).
    # Only --live copies count: a view-only dashboard someone opens for a look is not the one.
    @(Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match '^(tradebot|python|pythonw)\.exe$' -and $_.CommandLine -match 'tradebot' -and
                       $_.CommandLine -match "\b$command\b" -and $_.CommandLine -match '--live' })
}

function Start-Loop {
    $loop = Find-Tradebot "run"
    $why = "trading loop was not running"
    if ($loop.Count -gt 0) {
        $beat = Join-Path $root "tradebot.loop.heartbeat"
        if (-not (Test-Path $beat)) {
            return  # running, from a version without heartbeats: nothing to judge it by
        }
        $last = (Get-Item $beat).LastWriteTime
        $started = ($loop | Sort-Object CreationDate | Select-Object -Last 1).CreationDate
        if ($started -gt $last) {
            $last = $started  # started since the last beat: its first cycle gets the full time
        }
        $quiet = (Get-Date) - $last
        if ($quiet.TotalMinutes -lt $FrozenMinutes) {
            return  # running and cycling: nothing to do
        }
        $ids = ($loop | ForEach-Object { $_.ProcessId }) -join ", "
        foreach ($p in $loop) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue  # not the tree: MT5 stays
        }
        # gone for good before the new one starts: a stopped loop holds its lock until it has exited
        Wait-Process -Id ($loop | ForEach-Object { $_.ProcessId }) -Timeout 60 -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $minutes = [math]::Floor($quiet.TotalMinutes)
        $note = "The trading loop froze: no cycle for $minutes min (last at {0:HH:mm}). It was stopped and started again; check that MT5 is connected." -f $last
        Set-Content -Path (Join-Path $root "tradebot.loop.frozen") -Value $note -Encoding utf8
        $why = "trading loop frozen (no cycle for $minutes min, last {0:HH:mm:ss}): stopped process(es) $ids" -f $last
    }
    Start-Process -FilePath $exe -ArgumentList "-c", $Config, "run", "--live" -WorkingDirectory $root -WindowStyle Minimized
    Write-Line "${why}: started it ($Config)"
}

function Start-Dashboard([bool]$browser) {
    if (Test-Path (Join-Path $root "DASHBOARD_OFF")) {
        return  # kept closed on purpose
    }
    if ((Find-Tradebot "ui").Count -gt 0) {
        return  # running: nothing to do
    }
    $arguments = @("-c", $Config, "ui", "--live")
    if (-not $browser) {
        $arguments += "--no-browser"  # a dashboard that stopped comes back without a new browser tab
    }
    Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $root -WindowStyle Minimized
    Write-Line ("dashboard was not running: started it ({0})" -f $(if ($browser) { "opened in the browser" } else { "no browser tab" }))
}

try {
    if (Test-Path (Join-Path $root "OFF")) {
        exit 0  # kept off on purpose: nothing to log every 5 minutes
    }

    $justOpened = $false
    if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
        if (-not (Test-Path $Terminal)) {
            Write-Line "MT5 not found at ${Terminal}: nothing started"
            exit 1
        }
        Start-Process -FilePath $Terminal
        Write-Line "MT5 was not open: opened it; waiting $ConnectSeconds s for it to log in"
        Start-Sleep -Seconds $ConnectSeconds
        $justOpened = $true
    }

    Start-Loop
    Start-Dashboard $justOpened
} catch {
    Write-Line "failed: $($_.Exception.Message)"
    exit 1
}
