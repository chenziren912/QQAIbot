[CmdletBinding()]
param(
    [ValidateRange(1, 20)]
    [int]$Account = 1,
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,
    [ValidateRange(1, 30)]
    [int]$TimeoutMinutes = 15
)

$ErrorActionPreference = 'Stop'
$baseUrl = "http://127.0.0.1:$Port"
$healthUrl = "$baseUrl/healthz"
$loginUrl = "$baseUrl/v1/flow/accounts/$Account/login?timeout_seconds=$($TimeoutMinutes * 60)"
$verifyUrl = "$baseUrl/v1/flow/accounts/$Account/login/verify"
$cancelUrl = "$baseUrl/v1/flow/accounts/$Account/login/cancel"
$headers = @{}
$apiKey = [Environment]::GetEnvironmentVariable('FLOW_API_KEY', 'Process')
if (-not $apiKey) {
    $apiKey = [Environment]::GetEnvironmentVariable('FLOW_API_KEY', 'User')
}
if ($apiKey) {
    $headers.Authorization = "Bearer $apiKey"
}

try {
    Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 5 | Out-Null
} catch {
    Write-Host 'Flow API 尚未运行，正在启动一次。' -ForegroundColor Yellow
    & (Join-Path $PSScriptRoot 'flow-api-guard.ps1') -Once -Port $Port
    for ($attempt = 0; $attempt -lt 15; $attempt++) {
        try {
            Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 5 | Out-Null
            break
        } catch {
            if ($attempt -eq 14) { throw 'Flow API 无法启动，请先检查 flow-api.stderr.log。' }
            Start-Sleep -Seconds 1
        }
    }
}

Write-Host "将打开 Google Flow 账号 $Account 的专用 Chrome 资料目录。" -ForegroundColor Cyan
Write-Host '请在弹出的普通 Chrome 中正常登录 Google 并进入 Flow；完成后关闭整个专用 Chrome 窗口，服务随后会自动严格检测。' -ForegroundColor Cyan

try {
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        $health = Invoke-RestMethod -Uri $healthUrl -Headers $headers -TimeoutSec 5
        $worker = @($health.pool.workers | Where-Object { $_.account -eq $Account }) | Select-Object -First 1
        if ($worker -and $worker.active -ne $true -and [int]$worker.queued -eq 0) {
            break
        }
        if ($worker.stage -ne 'checking_readiness') {
            throw "账号 $Account 正在处理任务（阶段：$($worker.stage)），请等待任务完成后再修复登录。"
        }
        if ($attempt -eq 59) {
            throw "账号 $Account 的启动检测超过 60 秒仍未结束。"
        }
        Start-Sleep -Seconds 1
    }
    $result = Invoke-RestMethod -Method Post -Uri $loginUrl -Headers $headers -TimeoutSec 30
    if ($result.opened -ne $true -or $result.persistentProfile -ne $true) {
        throw 'Flow API 未能打开专用登录窗口。'
    }
    [void](Read-Host '完成登录并确认可以进入 Flow 后，请关闭整个专用 Chrome 窗口，再按 Enter 开始严格检测')
    $result = Invoke-RestMethod -Method Post -Uri $verifyUrl -Headers $headers -TimeoutSec 120
    if ($result.ready -ne $true -or $result.persistentProfile -ne $true) {
        throw 'Flow API 未确认登录可用。'
    }
    Write-Host "账号 $Account 登录已保存在专用持久浏览器并通过 Flow 工作区检测；不读取 Cookie 文件。" -ForegroundColor Green
} catch {
    $details = $_.ErrorDetails.Message
    if ($details) {
        try {
            $parsed = $details | ConvertFrom-Json
            if ($parsed.error.message) { $details = $parsed.error.message }
        } catch {}
    }
    if (-not $details) { $details = $_.Exception.Message }
    try { Invoke-RestMethod -Method Post -Uri $cancelUrl -Headers $headers -TimeoutSec 10 | Out-Null } catch {}
    Write-Host "Flow 登录修复失败：$details" -ForegroundColor Red
    exit 1
}
