# Opens MetaTrader 5 and starts the live trading loop, each only if it is not already running.
#
# Run by the "tradebot autostart" scheduled task at logon and every 15 minutes after, so a reboot
# (Windows Update, a power cut, a shutdown) or a stopped loop (Ctrl+C, a closed window, a crash)
# leaves the bot off for at most 15 minutes. Safe to run by hand at any time: it never starts a
# second terminal or a second loop (the loop also refuses a second copy in the same folder).
#
# MT5 is opened here, not by the bot: a terminal the bot's own process starts is that process's
# child, and stopping the bot would then close the terminal with it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_bot.ps1
#
# Pausing new entries: create a file named STOP in the tradingBot folder (the loop keeps running
# and still manages open trades). Keeping the bot off: create a file named OFF there; this script
# then starts nothing until it is deleted. Only actions are logged, to start_bot.log.

param(
    [string]$Terminal = "C:\Program Files\MetaTrader 5\terminal64.exe",
    [string]$Config = "config/live.local.json",
    [int]$ConnectSeconds = 45
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$log = Join-Path $root "start_bot.log"

function Write-Line([string]$text) {
    $line = "{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $text
    Add-Content -Path $log -Value $line -Encoding utf8
    Write-Host $line
}

try {
    if (Test-Path (Join-Path $root "OFF")) {
        exit 0  # kept off on purpose: nothing to log every 15 minutes
    }

    if (-not (Get-Process terminal64 -ErrorAction SilentlyContinue)) {
        if (-not (Test-Path $Terminal)) {
            Write-Line "MT5 not found at ${Terminal}: nothing started"
            exit 1
        }
        Start-Process -FilePath $Terminal
        Write-Line "MT5 was not open: opened it; waiting $ConnectSeconds s for it to log in"
        Start-Sleep -Seconds $ConnectSeconds
    }

    # however it was started: tradebot.exe, or python -m tradebot (as another tool may start it)
    $loop = Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match '^(tradebot|python|pythonw)\.exe$' -and $_.CommandLine -match 'tradebot' -and
                       $_.CommandLine -match '\brun\b' -and $_.CommandLine -match '--live' }
    if ($loop) {
        exit 0  # running: nothing to do
    }
    $exe = Join-Path $root ".venv\Scripts\tradebot.exe"
    Start-Process -FilePath $exe -ArgumentList "-c", $Config, "run", "--live" -WorkingDirectory $root -WindowStyle Normal
    Write-Line "trading loop was not running: started it ($Config)"
} catch {
    Write-Line "failed: $($_.Exception.Message)"
    exit 1
}
