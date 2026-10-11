[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$Reload,
    [string]$NapCatExecutable,
    [string]$DataDir,
    [ValidateRange(1, 65535)]
    [int]$FlowApiPort = 8787,
    [string]$FlowProjectDir,
    [switch]$NoFlowApi,
    [switch]$NoStart,
    [ValidateRange(5, 600)]
    [int]$StartupTimeoutSec = 60
)

$ErrorActionPreference = 'Stop'

function Quote-ProcessArgument {
    param([AllowEmptyString()][string]$Value)

    if ($Value -notmatch '[\s"]') {
        return $Value
    }
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-Health {
    param([string]$Url)

    try {
        $response = Invoke-RestMethod -Uri ($Url + '/healthz') -Method Get -TimeoutSec 3 -ErrorAction Stop
        if ($response -and $response.ok -eq $true) {
            return $response
        }
    } catch {
        # The server may still be starting, or the port may belong to another
        # program. The caller distinguishes those cases after the retry loop.
    }
    return $null
}

function Get-ConsoleReady {
    param([string]$Url)

    try {
        $response = Invoke-RestMethod -Uri ($Url + '/terminal/help') -Method Get -TimeoutSec 3 -ErrorAction Stop
        return [bool]($response -and $response.help)
    } catch {
        return $false
    }
}

function Show-ServerFailure {
    param([string]$DataRoot)

    Write-Host 'QQ 服务没有在规定时间内启动。' -ForegroundColor Red
    $logRoot = Join-Path $DataRoot 'logs'
    foreach ($name in @('console-server.stderr.log', 'console-server.stdout.log')) {
        $path = Join-Path $logRoot $name
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Write-Host "--- $name（最近 30 行）---" -ForegroundColor Yellow
            Get-Content -LiteralPath $path -Tail 30 | ForEach-Object { Write-Host $_ }
        }
    }
}

try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    Set-Location -LiteralPath $projectRoot

    if (-not $DataDir) {
        if (-not $env:LOCALAPPDATA) {
            throw '无法确定 LOCALAPPDATA；请使用 -DataDir 指定运行数据目录。'
        }
        $DataDir = Join-Path $env:LOCALAPPDATA 'QQAIGroupAgent\data'
    }
    if (-not (Test-Path -LiteralPath $DataDir)) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    }
    $DataDir = (Resolve-Path -LiteralPath $DataDir -ErrorAction Stop).Path
    $env:QQ_AI_DATA_DIR = $DataDir
    $baseUrl = "http://127.0.0.1:$Port"

    $health = Get-Health -Url $baseUrl
    $consoleReady = if ($health) { Get-ConsoleReady -Url $baseUrl } else { $false }
    if ($health -and -not $consoleReady) {
        throw "检测到 $baseUrl 正在运行旧版 QQ 服务；请先重启服务，再运行 scripts\console.ps1。"
    }
    $serverProcess = $null
    if (-not $health -and -not $NoStart) {
        $startScript = Join-Path $PSScriptRoot 'start.ps1'
        if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
            throw "找不到主启动脚本：$startScript"
        }
        $powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
        if (-not $powershell) {
            $powershell = Get-Command pwsh.exe -ErrorAction SilentlyContinue
        }
        if (-not $powershell) {
            throw '找不到 powershell.exe/pwsh.exe，无法自动启动 QQ 服务。'
        }

        $argumentParts = @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', (Quote-ProcessArgument $startScript),
            '-Port', "$Port",
            '-DataDir', (Quote-ProcessArgument $DataDir),
            '-FlowApiPort', "$FlowApiPort"
        )
        if ($Reload) {
            $argumentParts += '-Reload'
        }
        if ($NapCatExecutable) {
            $argumentParts += '-NapCatExecutable'
            $argumentParts += (Quote-ProcessArgument $NapCatExecutable)
        }
        if ($FlowProjectDir) {
            $argumentParts += '-FlowProjectDir'
            $argumentParts += (Quote-ProcessArgument $FlowProjectDir)
        }
        if ($NoFlowApi) {
            $argumentParts += '-NoFlowApi'
        }

        $logRoot = Join-Path $DataDir 'logs'
        if (-not (Test-Path -LiteralPath $logRoot)) {
            New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
        }
        $stdoutLog = Join-Path $logRoot 'console-server.stdout.log'
        $stderrLog = Join-Path $logRoot 'console-server.stderr.log'
        Write-Host '未检测到 QQ 服务，正在后台启动；终端控制台会在健康检查通过后开放。' -ForegroundColor Yellow
        try {
            $serverProcess = Start-Process -FilePath $powershell.Source `
                -ArgumentList ($argumentParts -join ' ') `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden `
                -RedirectStandardOutput $stdoutLog `
                -RedirectStandardError $stderrLog `
                -PassThru
        } catch {
            # Some older Windows PowerShell builds reject redirect targets that
            # are already open. Keep startup usable and retry without logs.
            $serverProcess = Start-Process -FilePath $powershell.Source `
                -ArgumentList ($argumentParts -join ' ') `
                -WorkingDirectory $projectRoot `
                -WindowStyle Hidden `
                -PassThru
        }

        $deadline = (Get-Date).AddSeconds($StartupTimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $health = Get-Health -Url $baseUrl
            $consoleReady = if ($health) { Get-ConsoleReady -Url $baseUrl } else { $false }
            if ($health -and $consoleReady) {
                break
            }
            if ($serverProcess -and $serverProcess.HasExited) {
                Start-Sleep -Milliseconds 250
                $health = Get-Health -Url $baseUrl
                $consoleReady = if ($health) { Get-ConsoleReady -Url $baseUrl } else { $false }
                if (-not $health -or -not $consoleReady) {
                    Show-ServerFailure -DataRoot $DataDir
                    if ($health) {
                        throw "QQ 服务已启动，但未提供终端接口；请确认启动的是本次更新后的代码。"
                    }
                    throw "QQ 服务进程已退出，退出代码：$($serverProcess.ExitCode)"
                }
                break
            }
            Start-Sleep -Milliseconds 500
        }
    }

    if (-not $health -or -not $consoleReady) {
        if ($NoStart) {
            throw "未连接到 $baseUrl；请先运行 scripts\start.ps1，或去掉 -NoStart 让控制台自动启动服务。"
        }
        Show-ServerFailure -DataRoot $DataDir
        throw "在 ${StartupTimeoutSec} 秒内未能连接到 $baseUrl。"
    }

    Write-Host "QQ AI 终端控制台已连接：$baseUrl" -ForegroundColor Green
    Write-Host "OneBot：$(if ($health.onebot_connected) { '已连接' } else { '等待 NapCat 连接' })"
    Write-Host "数据目录：$DataDir"
    Write-Host '输入 /help 查看全部中文命令；输入 /quit 退出控制台（不会停止服务）。'

    while ($true) {
        Write-Host -NoNewline 'qq-ai> '
        $line = [Console]::ReadLine()
        if ($null -eq $line) {
            break
        }
        if (-not $line.Trim()) {
            continue
        }
        if ($line.Trim() -match '^/(clear|清屏)\s*$') {
            Clear-Host
            continue
        }

        $body = @{ command = $line } | ConvertTo-Json -Compress
        try {
            $request = Invoke-RestMethod `
                -Uri ($baseUrl + '/terminal/command') `
                -Method Post `
                -Headers @{ 'X-QQAI-Terminal' = '1' } `
                -ContentType 'application/json; charset=utf-8' `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) `
                -TimeoutSec 900 `
                -ErrorAction Stop
            if ($request.output) {
                Write-Host ([string]$request.output)
            }
            if ($request.exit -eq $true) {
                break
            }
        } catch {
            Write-Host "终端命令失败：$($_.Exception.Message)" -ForegroundColor Red
            Write-Host '控制台仍在运行；输入 /help 查看命令，或再次重试。' -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "`n终端控制台启动失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
