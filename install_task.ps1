<#
.SYNOPSIS
  安装基金监控定时任务
.DESCRIPTION
  创建 3 个 Windows 计划任务（仅交易日周一到周五运行）：
    1. 全球股市简报 — 每天 9:30 推送（global_briefing.py）
    2. 基金晚报 — 每天 15:30 发送收盘晚报（fund_watch.py）
    3. 基金盘中监控 — 每天 9:25 启动，每 10 分钟轮询（fund_monitor.py）
#>

$PythonPath = (Get-Command python).Source
$ScriptDir = $PSScriptRoot

if (-not $PythonPath) {
    Write-Error "未找到 Python，请先安装"
    exit 1
}

function New-TaskIfMissing {
    param($Name, $Script, $TriggerTime, $Desc)

    $ScriptPath = "$ScriptDir\$Script"
    $Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "`"$ScriptPath`"" -WorkingDirectory $ScriptDir
    $Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $TriggerTime
    $Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 8)

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  更新任务「$Name」..."
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }

    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -Settings $Settings -RunLevel Limited -User $env:USERNAME
    Write-Host "  ✅ $Name — 每天 $TriggerTime 运行「$Desc」"
}

Write-Host "📅 安装基金监控定时任务"
Write-Host ""

New-TaskIfMissing -Name "全球股市简报" -Script "global_briefing.py" -TriggerTime "09:30" -Desc "全球股市早报"
New-TaskIfMissing -Name "基金晚报" -Script "fund_watch.py" -TriggerTime "15:30" -Desc "收盘晚报推送"
New-TaskIfMissing -Name "基金盘中监控" -Script "fund_monitor.py" -TriggerTime "09:25" -Desc "盘中实时监控"

Write-Host ""
Write-Host "设置环境变量（推荐）:"
Write-Host '  [System.Environment]::SetEnvironmentVariable("WECHAT_WEBHOOK", "你的企业微信机器人URL", "User")'
Write-Host ""
Write-Host "手动测试:"
Write-Host "  python `"$ScriptDir\global_briefing.py`""
Write-Host "  python `"$ScriptDir\fund_watch.py`""
Write-Host "  python `"$ScriptDir\fund_monitor.py`""
Write-Host ""
Write-Host "卸载:"
Write-Host "  Unregister-ScheduledTask -TaskName '全球股市简报' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName '基金晚报' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName '基金盘中监控' -Confirm:`$false"
