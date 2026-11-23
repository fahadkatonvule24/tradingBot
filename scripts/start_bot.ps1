# Opens MetaTrader 5 and starts the live trading loop, each only if it is not already running.
#
# Run at logon by the "tradebot autostart" scheduled task, so a reboot (Windows Update, a power cut,
# a shutdown) no longer leaves the bot off. Safe to run by hand at any time: it never starts a
# second terminal or a second loop (the loop also refuses a second copy in the same folder).
#
# MT5 is opened here, not by the bot: a terminal the bot's own process starts is that process's
# child, and stopping the bot would then close the terminal with it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_bot.ps1
#
# Pausing: create a file named STOP in the tradingBot folder (no new entries; trades are still
# managed). Closing the loop's window stops it until the next logon or the next run of this script.

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
    if (Get-Process terminal64 -ErrorAction SilentlyContinue) {
        Write-Line "MT5 already open"
    } elseif (-not (Test-Path $Terminal)) {
        Write-Line "MT5 not found at ${Terminal}: nothing started"
        exit 1
    } else {
        Start-Process -FilePath $Terminal
        Write-Line "MT5 opened; waiting $ConnectSeconds s for it to log in"
        Start-Sleep -Seconds $ConnectSeconds
    }

    $loop = Get-CimInstance Win32_Process -Filter "Name='tradebot.exe'" |
        Where-Object { $_.CommandLine -match "\brun\b" -and $_.CommandLine -match "--live" }
    if ($loop) {
        Write-Line "trading loop already running (process $($loop.ProcessId -join ', '))"
        exit 0
    }
    $exe = Join-Path $root ".venv\Scripts\tradebot.exe"
    Start-Process -FilePath $exe -ArgumentList "-c", $Config, "run", "--live" -WorkingDirectory $root -WindowStyle Normal
    Write-Line "trading loop started ($Config)"
} catch {
    Write-Line "failed: $($_.Exception.Message)"
    exit 1
}
