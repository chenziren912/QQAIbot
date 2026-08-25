[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$Reload,
    [string]$NapCatExecutable,
    [string]$DataDir,
    [switch]$KeepOpenOnError
)

$ErrorActionPreference = 'Stop'

function Get-ProcessSummary {
    param([int]$ProcessId)

    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if ($process) {
            return "$($process.Name) (PID $ProcessId)"
        }
    } catch {
        # The owning process may already have exited.  The port check below
        # still gives a useful error without process metadata.
    }
    return "PID $ProcessId"
}

try {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    Set-Location -LiteralPath $projectRoot

    if (-not $DataDir) {
        if (-not $env:LOCALAPPDATA) {
            throw '无法确定 LOCALAPPDATA；请使用 -DataDir 指定一个位于健康 NTFS 磁盘的数据目录。'
        }
        $DataDir = Join-Path $env:LOCALAPPDATA 'QQAIGroupAgent\data'
    }
    $resolvedDataParent = Split-Path -Parent $DataDir
    if (-not (Test-Path -LiteralPath $resolvedDataParent)) {
        New-Item -ItemType Directory -Path $resolvedDataParent -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $DataDir)) {
        New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
    }
    $env:QQ_AI_DATA_DIR = (Resolve-Path -LiteralPath $DataDir -ErrorAction Stop).Path

    # Do this before invoking Uvicorn.  Otherwise a second launch prints a
    # low-level WinSock 10048 error and the generic catch below can misleadingly
    # suggest that Python packages are missing.
    $listeners = @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -gt 0) {
        $health = $null
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 3
        } catch {
            # A listener without this app's health endpoint is handled below.
        }
        if ($health -and $health.ok -eq $true) {
            Write-Host "QQ 群 AI 代理已在运行：http://127.0.0.1:$Port" -ForegroundColor Green
            if ($health.onebot_connected) {
                Write-Host 'OneBot：已连接'
            } else {
                Write-Host 'OneBot：等待 NapCat 连接' -ForegroundColor Yellow
            }
            Write-Host "运行数据目录：$env:QQ_AI_DATA_DIR"
            return
        }
        $owner = $listeners[0].OwningProcess
        $description = Get-ProcessSummary -ProcessId $owner
        throw "端口 127.0.0.1:$Port 已被 $description 占用，且不是 QQ 群 AI 代理。请关闭该程序，或使用 -Port 指定其他端口。"
    }

    # The service may be launched from Task Scheduler or a different shell
    # whose PATH does not contain the interactive user's ffmpeg directory.
    # Export an explicit path when PowerShell can resolve it, with the same
    # conventional-root fallback used by app.service.
    $ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ffmpegCommand) {
        $env:FFMPEG_PATH = $ffmpegCommand.Source
    } else {
        $ffmpegCandidate = Get-ChildItem -LiteralPath 'C:\' -Filter 'ffmpeg*' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'bin\ffmpeg.exe' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
        if ($ffmpegCandidate) {
            $env:FFMPEG_PATH = $ffmpegCandidate
        }
    }

    # yt-dlp's current YouTube EJS challenge solver needs a JavaScript
    # runtime.  Pin the discovered Node executable for hidden/task-scheduler
    # launches so the service does not depend on an interactive PATH.
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        $env:NODE_PATH = $nodeCommand.Source
        $nodeDirectory = Split-Path -Parent $nodeCommand.Source
        if ($nodeDirectory -and (($env:Path -split ';') -notcontains $nodeDirectory)) {
            $env:Path = "$nodeDirectory;$env:Path"
        }
    }

    if ($NapCatExecutable) {
        $resolvedNapCat = Resolve-Path -LiteralPath $NapCatExecutable -ErrorAction Stop
        Write-Host "正在启动 QQNT / NapCat: $resolvedNapCat"
        Start-Process -FilePath $resolvedNapCat.Path
    }

    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $python = $venvPython
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw '未找到 Python 3.10+。请安装 Python 3.12，或在项目根目录创建 .venv。'
        }
        $pythonVersion = & $pythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        $versionParts = $pythonVersion.Trim() -split '\.'
        $major = 0
        $minor = 0
        if ($versionParts.Count -ge 2) {
            [void][int]::TryParse($versionParts[0], [ref]$major)
            [void][int]::TryParse($versionParts[1], [ref]$minor)
        }
        if (($major -gt 3) -or ($major -eq 3 -and $minor -ge 10)) {
            $python = $pythonCommand.Source
        } else {
            $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
            $python = $null
            if ($pyLauncher) {
                foreach ($requestedVersion in @('3.14', '3.13', '3.12', '3.11', '3.10')) {
                    try {
                        $candidate = & $pyLauncher.Source ("-" + $requestedVersion) -c "import sys; print(sys.executable)" 2>$null
                        if ($candidate -and (Test-Path -LiteralPath $candidate.Trim())) {
                            $python = $candidate.Trim()
                            break
                        }
                    } catch {
                        # Try the next installed Python version.
                    }
                }
            }
            if (-not $python) {
                throw "当前 Python 是 $pythonVersion；本项目及最新版 yt-dlp 需要 Python 3.10+。请安装 Python 3.12，或创建 .venv。"
            }
        }
    }

    # Make the selected interpreter's directory visible to the service too.
    # This lets _find_local_executable resolve the matching yt-dlp.exe in a
    # venv instead of silently falling back to a different global install.
    $pythonDirectory = Split-Path -Parent $python
    if ($pythonDirectory -and (($env:Path -split ';') -notcontains $pythonDirectory)) {
        $env:Path = "$pythonDirectory;$env:Path"
    }

    $arguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$Port")
    if ($Reload) {
        $arguments += '--reload'
    }

    Write-Host "QQ 群 AI 代理将运行在 http://127.0.0.1:$Port"
    Write-Host "运行数据目录：$env:QQ_AI_DATA_DIR"
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "服务进程以退出代码 $LASTEXITCODE 结束。"
    }
} catch {
    $failure = $_.Exception.Message
    Write-Host "`n启动失败：$failure" -ForegroundColor Red
    if ($failure -match 'No module named|ModuleNotFoundError|uvicorn') {
        Write-Host '缺少 Python 依赖时，请使用 Python 3.10+ 在项目根目录运行：py -3.12 -m pip install -e ".[dev]"' -ForegroundColor Yellow
    } else {
        Write-Host '请根据上方的具体错误处理；这次不是通用的 Python 安装问题。' -ForegroundColor Yellow
    }
    if ($KeepOpenOnError -and [Environment]::UserInteractive) {
        [void](Read-Host '按 Enter 关闭此窗口')
    }
    exit 1
}
