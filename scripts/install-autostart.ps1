[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [ValidateRange(1, 65535)]
    [int]$FlowApiPort = 8787,
    [string]$NapCatExecutable,
    [string]$FlowProjectDir,
    [string]$FlowCookiesFile,
    [string]$FlowCookiesFile2,
    [switch]$NoFlowApi,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$taskName = 'QQ 群 AI 代理'
$flowTaskName = "$taskName - Google Flow API"

function Remove-TaskIfPresent {
    param([string]$Name)

    $existing = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "已移除计划任务：$Name"
    } else {
        Write-Host "未找到计划任务：$Name"
    }
}

if ($Remove) {
    Remove-TaskIfPresent -Name $taskName
    Remove-TaskIfPresent -Name $flowTaskName
    exit 0
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$startScript = Resolve-Path -LiteralPath (Join-Path $projectRoot 'scripts\start.ps1') -ErrorAction Stop
$flowGuardScript = Resolve-Path -LiteralPath (Join-Path $projectRoot 'scripts\flow-api-guard.ps1') -ErrorAction Stop
$powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source

$argumentParts = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', ('"{0}"' -f $startScript.Path),
    '-Port', "$Port",
    '-FlowApiPort', "$FlowApiPort"
)

if ($NapCatExecutable) {
    $resolvedNapCat = Resolve-Path -LiteralPath $NapCatExecutable -ErrorAction Stop
    $argumentParts += '-NapCatExecutable'
    $argumentParts += ('"{0}"' -f $resolvedNapCat.Path)
}

if ($FlowProjectDir) {
    $resolvedFlowProject = Resolve-Path -LiteralPath $FlowProjectDir -ErrorAction Stop
    $argumentParts += '-FlowProjectDir'
    $argumentParts += ('"{0}"' -f $resolvedFlowProject.Path)
}

if ($FlowCookiesFile) {
    $resolvedFlowCookies = Resolve-Path -LiteralPath $FlowCookiesFile -ErrorAction Stop
    $argumentParts += '-FlowCookiesFile'
    $argumentParts += ('"{0}"' -f $resolvedFlowCookies.Path)
}

if ($FlowCookiesFile2) {
    $resolvedFlowCookies2 = Resolve-Path -LiteralPath $FlowCookiesFile2 -ErrorAction Stop
    $argumentParts += '-FlowCookiesFile2'
    $argumentParts += ('"{0}"' -f $resolvedFlowCookies2.Path)
}

if ($NoFlowApi) {
    $argumentParts += '-NoFlowApi'
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

if ($NoFlowApi) {
    Remove-TaskIfPresent -Name $flowTaskName
    Write-Host '已按 -NoFlowApi 禁用 Google Flow API 自动启动。'
    exit 0
}

$flowArgumentParts = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-WindowStyle', 'Hidden',
    '-File', ('"{0}"' -f $flowGuardScript.Path),
    '-Guard',
    '-Port', "$FlowApiPort"
)
if ($FlowProjectDir) {
    $flowArgumentParts += '-FlowProjectDir'
    $flowArgumentParts += ('"{0}"' -f $resolvedFlowProject.Path)
}
if ($FlowCookiesFile) {
    $flowArgumentParts += '-FlowCookiesFile'
    $flowArgumentParts += ('"{0}"' -f $resolvedFlowCookies.Path)
}
if ($FlowCookiesFile2) {
    $flowArgumentParts += '-FlowCookiesFile2'
    $flowArgumentParts += ('"{0}"' -f $resolvedFlowCookies2.Path)
}

$flowAction = New-ScheduledTaskAction -Execute $powershellExe -Argument ($flowArgumentParts -join ' ')
$flowTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$flowSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $flowTaskName -Action $flowAction -Trigger $flowTrigger -Principal $principal -Settings $flowSettings -Force | Out-Null
Write-Host "已创建计划任务：$flowTaskName"
Write-Host "它会在登录后启动并持续守护 http://127.0.0.1:$FlowApiPort/healthz；Flow 项目或 Node 尚未就绪时会自动重试。"
if ($FlowProjectDir) {
    Write-Host "Flow 项目目录：$resolvedFlowProject"
} else {
    Write-Host 'Flow 项目目录将按 FLOW_VIDEO_PROJECT_DIR / GOOGLE_FLOW_VIDEO_PROJECT 环境变量或内置默认路径查找。'
}
