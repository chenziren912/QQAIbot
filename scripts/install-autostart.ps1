[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [string]$NapCatExecutable,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'QQ 群 AI 代理'

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "已移除计划任务：$taskName"
    } else {
        Write-Host "未找到计划任务：$taskName"
    }
    exit 0
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Resolve-Path -LiteralPath (Join-Path $projectRoot 'scripts\start.ps1') -ErrorAction Stop
$powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source

$argumentParts = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', ('"{0}"' -f $startScript.Path),
    '-Port', $Port
)

if ($NapCatExecutable) {
    $resolvedNapCat = Resolve-Path -LiteralPath $NapCatExecutable -ErrorAction Stop
    $argumentParts += '-NapCatExecutable'
    $argumentParts += ('"{0}"' -f $resolvedNapCat.Path)
}

$action = New-ScheduledTaskAction -Execute $powershellExe -Argument ($argumentParts -join ' ')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType InteractiveToken -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "已创建计划任务：$taskName"
Write-Host '它会在当前用户登录后启动本机服务。'
if ($NapCatExecutable) {
    Write-Host '它也会先启动指定的 QQNT / NapCat 可执行文件；QQ 登录仍需由 QQ 客户端扫码完成。'
} else {
    Write-Host '未指定 -NapCatExecutable；请让 QQNT/NapCat 自行随系统启动，或重新运行本脚本并提供该路径。'
}
