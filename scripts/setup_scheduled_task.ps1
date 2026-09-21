# Registers a Windows Scheduled Task that runs the daily fetch script
# every trading day (Mon-Fri) at 20:00.
# Why 20:00: ezmoney.com.tw (Uni-President) usually publishes its PCF list after 16:30;
# the other three issuers publish between 14:45-16:30, and TWSE/TPEx/TAIFEX end-of-day
# data is complete well before 20:00, so 20:00 covers every source.
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File .\scripts\setup_scheduled_task.ps1

$ErrorActionPreference = "Stop"

$taskName = "TW-ActiveETF-DailyFetch"
$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot "scripts\fetch_daily.py"

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pythonCmd = Get-Command py -ErrorAction SilentlyContinue
}
if (-not $pythonCmd) {
    Write-Error "python not found on PATH. Install Python or add it to PATH first."
    exit 1
}
$pythonPath = $pythonCmd.Source

$dataDir = Join-Path $projectRoot "data"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$logPath = Join-Path $dataDir "fetch_task.log"

# cmd /c 遇到「開頭是引號、且整串裡引號超過兩個」時會把第一個和最後一個引號吃掉，
# 指令就變成壞掉的字串(立刻結束、exit code 1、log 完全沒有輸出)。所以整串指令
# 外面要再包一層引號。這個坑讓排程從建立以來一次都沒有真的跑成功過。
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$pythonPath`" `"$scriptPath`" >> `"$logPath`" 2>&1`"" `
    -WorkingDirectory $projectRoot

$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 20:00

# 預設「使用電池時不啟動」會讓筆電用電池時整個排程被跳過，所以明確允許；
# StartWhenAvailable：20:00 電腦沒開機/睡眠時，下次可以執行時自動補跑一次。
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily fetch of TW active ETF PCF data (tw-active-etf-tracker)" -Force

Write-Host "Scheduled task '$taskName' created: runs Mon-Fri at 20:00."
Write-Host "Test it now with:  schtasks /Run /TN `"$taskName`""
Write-Host "Log file: $logPath"
