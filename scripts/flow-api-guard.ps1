[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,
    [string]$FlowProjectDir,
    [string]$DataDir,
    [ValidateRange(1, 300)]
    [int]$PollSeconds = 5,
    [switch]$Guard,
    [switch]$Once
)

# This helper deliberately keeps the Flow API process separate from the
# Python service.  A crashed browser/API child can therefore be restarted
# without restarting the QQ agent itself.
$ErrorActionPreference = 'Stop'
$scriptPath = $MyInvocation.MyCommand.Path

function Import-UserFlowSessionEnvironment {
    # Import profile paths only; legacy authentication exports are never used.
    $profileNames = @('FLOW_PROFILE_ROOT', 'FLOW_USER_DATA_DIR', 'FLOW_USER_DATA_DIRS', 'FLOW_ACCOUNT_COUNT', 'FLOW_PERSISTENT_PROFILES')
    $profileNames += (1..20 | ForEach-Object { "FLOW_USER_DATA_DIR_$_" })
    foreach ($name in $profileNames) {
        if ([Environment]::GetEnvironmentVariable($name, 'Process')) {
            continue
        }
        $userValue = [Environment]::GetEnvironmentVariable($name, 'User')
        if ($userValue) {
            [Environment]::SetEnvironmentVariable($name, $userValue, 'Process')
        }
    }
}

function Write-FlowLog {
    param([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::DarkGray)

    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [Flow] $Message"
    try {
        if ($script:LogPath) {
            Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
        }
    } catch {
        # Logging must never stop the watchdog.
    }
    if (-not $Guard) {
        Write-Host $line -ForegroundColor $Color
    }
}

function Initialize-LogPath {
    $base = $DataDir
    if (-not $base) {
        $base = $env:QQ_AI_DATA_DIR
    }
    if (-not $base -and $env:LOCALAPPDATA) {
        $base = Join-Path $env:LOCALAPPDATA 'QQAIGroupAgent\data'
    }
    if (-not $base) {
        return
    }
    try {
        $logDirectory = Join-Path $base 'logs'
        New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
        $script:LogPath = Join-Path $logDirectory 'flow-api-guard.log'
    } catch {
        $script:LogPath = $null
    }
}

function Get-FlowProject {
    $explicit = $FlowProjectDir
    if (-not $explicit) {
        $explicit = $env:FLOW_VIDEO_PROJECT_DIR
    }
    if (-not $explicit) {
        $explicit = $env:GOOGLE_FLOW_VIDEO_PROJECT
    }

    $candidates = @()
    if ($explicit) {
        $candidates += $explicit
    } else {
        $scriptsDirectory = Split-Path -Parent $scriptPath
        $repositoryParent = Split-Path -Parent (Split-Path -Parent $scriptsDirectory)
        $candidates += (Join-Path $repositoryParent 'Google Flow Video Gen to AI Skill\google-flow-video-skill\mcp')
        $candidates += 'D:\项目\Google Flow Video Gen to AI Skill\google-flow-video-skill\mcp'
        if ($env:USERPROFILE) {
            $candidates += (Join-Path $env:USERPROFILE 'Google Flow Video Gen to AI Skill\google-flow-video-skill\mcp')
        }
    }

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (-not $candidate) {
            continue
        }
        try {
            $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
            if ((Test-Path -LiteralPath (Join-Path $resolved 'package.json') -PathType Leaf) -and
                (Test-Path -LiteralPath (Join-Path $resolved 'src\api-server.js') -PathType Leaf)) {
                return $resolved
            }
        } catch {
            # Try the next configured/default candidate.
        }
    }
    return $null
}

function Get-NodePath {
    if ($env:FLOW_NODE_PATH) {
        try {
            $resolved = (Resolve-Path -LiteralPath $env:FLOW_NODE_PATH -ErrorAction Stop).Path
            if (Test-Path -LiteralPath $resolved -PathType Leaf) {
                return $resolved
            }
        } catch {
            # Fall back to PATH below.
        }
    }
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        if ($nodeCommand.Path) {
            return $nodeCommand.Path
        }
        if ($nodeCommand.Source) {
            return $nodeCommand.Source
        }
    }
    return $null
}

function Ensure-FlowBrowserExecutable {
    if ($env:FLOW_BROWSER_EXECUTABLE_PATH) {
        if (Test-Path -LiteralPath $env:FLOW_BROWSER_EXECUTABLE_PATH -PathType Leaf) {
            return $true
        }
        Write-FlowLog "FLOW_BROWSER_EXECUTABLE_PATH 不存在：$env:FLOW_BROWSER_EXECUTABLE_PATH；尝试自动查找 Chromium。" ([ConsoleColor]::Yellow)
        $env:FLOW_BROWSER_EXECUTABLE_PATH = $null
    }

    $candidates = @(
        'C:\Program Files\Google\Chrome\Application\chrome.exe',
        'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
    )
    if ($env:LOCALAPPDATA) {
        $candidates += (Join-Path $env:LOCALAPPDATA 'Google\Chrome SxS\Application\chrome.exe')
        $candidates += (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    }
    $candidates += 'C:\Program Files\Microsoft\Edge\Application\msedge.exe'

    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $env:FLOW_BROWSER_EXECUTABLE_PATH = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
            Write-FlowLog "已发现 Chromium 浏览器：$env:FLOW_BROWSER_EXECUTABLE_PATH" ([ConsoleColor]::Green)
            return $true
        }
    }
    return $false
}

function Get-SessionDataDirectory {
    $base = $DataDir
    if (-not $base) {
        $base = $env:QQ_AI_DATA_DIR
    }
    if (-not $base -and $env:LOCALAPPDATA) {
        $base = Join-Path $env:LOCALAPPDATA 'QQAIGroupAgent\data'
    }
    if (-not $base) {
        return $null
    }
    try {
        New-Item -ItemType Directory -Path $base -Force | Out-Null
        return (Resolve-Path -LiteralPath $base -ErrorAction Stop).Path
    } catch {
        return $null
    }
}

function Ensure-FlowProfileRoot {
    if ($env:FLOW_PROFILE_ROOT) {
        try {
            New-Item -ItemType Directory -Path $env:FLOW_PROFILE_ROOT -Force | Out-Null
            $env:FLOW_PROFILE_ROOT = (Resolve-Path -LiteralPath $env:FLOW_PROFILE_ROOT -ErrorAction Stop).Path
            return $true
        } catch {
            Write-FlowLog "FLOW_PROFILE_ROOT 无法使用：$($_.Exception.Message)" ([ConsoleColor]::Red)
            return $false
        }
    }

    $sessionDirectory = Get-SessionDataDirectory
    if (-not $sessionDirectory) {
        Write-FlowLog '无法确定 Flow 持久浏览器资料目录；请设置 FLOW_PROFILE_ROOT。' ([ConsoleColor]::Red)
        return $false
    }
    try {
        $profileRoot = Join-Path $sessionDirectory 'flow-browser-profiles'
        New-Item -ItemType Directory -Path $profileRoot -Force | Out-Null
        $env:FLOW_PROFILE_ROOT = (Resolve-Path -LiteralPath $profileRoot -ErrorAction Stop).Path
        if (-not $env:FLOW_PERSISTENT_PROFILES) {
            $env:FLOW_PERSISTENT_PROFILES = '1'
        }
        Write-FlowLog 'Google Flow 已启用独立持久浏览器资料；刷新后的登录状态会跨重启保留。' ([ConsoleColor]::Green)
        return $true
    } catch {
        Write-FlowLog "创建 Flow 持久浏览器资料目录失败：$($_.Exception.Message)" ([ConsoleColor]::Red)
        return $false
    }
}

function Ensure-FlowSession {
    Import-UserFlowSessionEnvironment
    # Only clear child-process configuration. Never delete old exports or the
    # authentication already stored in the persistent browser directories.
    foreach ($name in @(
        'FLOW_STORAGE_STATE', 'FLOW_STORAGE_STATE_1', 'FLOW_STORAGE_STATE_2', 'FLOW_STORAGE_STATES',
        'FLOW_COOKIES_FILE', 'FLOW_COOKIES_FILE_1', 'FLOW_COOKIES_FILE_2', 'FLOW_COOKIES_FILES'
    )) { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    $env:FLOW_PERSISTENT_PROFILES = '1'
    if (-not (Ensure-FlowProfileRoot)) { return $false }
    $listed = @($env:FLOW_USER_DATA_DIRS -split '[;\r\n]+' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $accountCount = 2
    if ($env:FLOW_ACCOUNT_COUNT) {
        if ($env:FLOW_ACCOUNT_COUNT -notmatch '^\d+$') { throw 'FLOW_ACCOUNT_COUNT 必须是 1 到 20 的整数' }
        $accountCount = [int]$env:FLOW_ACCOUNT_COUNT
        if ($accountCount -lt 1 -or $accountCount -gt 20) { throw 'FLOW_ACCOUNT_COUNT 必须是 1 到 20 的整数' }
    }
    foreach ($entry in (Get-ChildItem Env:)) {
        if ($entry.Name -match '^FLOW_USER_DATA_DIR_(\d+)$' -and $entry.Value) {
            $accountCount = [Math]::Max($accountCount, [int]$Matches[1])
        }
    }
    $accountCount = [Math]::Max($accountCount, $listed.Count)
    if ($accountCount -gt 20) { throw 'Flow 持久浏览器资料最多支持 20 个账号' }
    $profileDirs = @()
    for ($index = 1; $index -le $accountCount; $index++) {
        $name = if ($index -eq 1) { 'FLOW_USER_DATA_DIR' } else { "FLOW_USER_DATA_DIR_$index" }
        $directory = [Environment]::GetEnvironmentVariable($name, 'Process')
        if (-not $directory -and $index -eq 1) { $directory = $env:FLOW_USER_DATA_DIR_1 }
        if (-not $directory -and $listed.Count -ge $index) { $directory = $listed[$index - 1] }
        if (-not $directory) { $directory = Join-Path $env:FLOW_PROFILE_ROOT "account-$index" }
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $resolvedDirectory = (Resolve-Path -LiteralPath $directory -ErrorAction Stop).Path
        if ($profileDirs -contains $resolvedDirectory) { throw '每个 Flow 账号必须使用不同的持久浏览器资料目录' }
        $profileDirs += $resolvedDirectory
        [Environment]::SetEnvironmentVariable($name, $resolvedDirectory, 'Process')
    }
    $env:FLOW_USER_DATA_DIR_1 = $null
    $env:FLOW_USER_DATA_DIRS = $profileDirs -join ';'
    $env:FLOW_ACCOUNT_COUNT = "$accountCount"
    Write-FlowLog "Flow 仅使用 $accountCount 个账号的独立持久浏览器资料；不读取或导入 Cookie/storage-state 文件。" ([ConsoleColor]::Green)
    return $true
}

function Get-FlowHeaders {
    $headers = @{}
    if ($env:FLOW_API_KEY) {
        $headers['Authorization'] = 'Bearer ' + $env:FLOW_API_KEY
    }
    return $headers
}

function Get-FlowHealth {
    param(
        [switch]$Deep,
        [int]$TimeoutSeconds = 2
    )

    try {
        $path = if ($Deep) { 'readyz' } else { 'healthz' }
        return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/$path" -Headers (Get-FlowHeaders) -TimeoutSec $TimeoutSeconds -ErrorAction Stop
    } catch {
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            try {
                return ($_.ErrorDetails.Message | ConvertFrom-Json -ErrorAction Stop)
            } catch {
                # Fall through to the generic unreachable result below.
            }
        }
        return $null
    }
}

function Test-FlowHealth {
    $response = Get-FlowHealth
    return ($response -and $response.ok -eq $true -and
        $response.runtime -eq 'qqbot-ai-browser' -and
        [int]$response.runtime_version -ge 5 -and
        $response.browser_resources.limit -eq 1 -and
        $response.browser_resources.lazy_start -eq $true -and
        $response.browser_resources.release_after_task -eq $true -and
        $response.authentication_mode -eq 'persistent_browser_profile' -and
        $response.cookie_import_enabled -eq $false -and
        $response.storage_state_import_enabled -eq $false -and
        $response.ai_browser_control -eq $true -and
        $response.submission_confirmation -eq 'flow_page_dom')
}

function Remove-IdleLegacyFlowApi {
    param($Health)

    # Upgrade only a positively identified legacy companion or this checkout's
    # outdated AI runtime. Unknown listeners and busy/unobservable jobs stay live.
    if (-not $Health -or $Health.ok -ne $true -or
        ($Health.runtime -eq 'qqbot-ai-browser' -and [int]$Health.runtime_version -ge 5)) {
        return $false
    }
    try {
        if (-not $Health.pool -or $null -eq $Health.pool.busy_workers -or
            $null -eq $Health.pool.queued_attempts -or
            [int]$Health.pool.busy_workers -gt 0 -or [int]$Health.pool.queued_attempts -gt 0) {
            Write-FlowLog '旧 Flow 服务仍有任务或无法确认空闲，暂不迁移；等待任务结束后守护会重试。' ([ConsoleColor]::Yellow)
            return $false
        }
        if ($Health.runtime -eq 'qqbot-ai-browser' -and
            (-not $Health.browser_resources -or $null -eq $Health.browser_resources.active -or
             $null -eq $Health.browser_resources.queued -or [int]$Health.browser_resources.active -gt 0 -or
             [int]$Health.browser_resources.queued -gt 0)) {
            Write-FlowLog 'Flow 浏览器仍在使用或无法确认已释放，等待任务结束后再升级。' ([ConsoleColor]::Yellow)
            return $false
        }
        $videos = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/v1/videos?limit=100" -Headers (Get-FlowHeaders) -TimeoutSec 3 -ErrorAction Stop
        if ($null -eq $videos.data -or @($videos.data | Where-Object { $_.status -in @('queued', 'in_progress') }).Count) {
            Write-FlowLog '旧 Flow 服务有未完成任务或任务状态无法核实，暂不迁移。' ([ConsoleColor]::Yellow)
            return $false
        }
        $owners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
        if ($owners.Count -ne 1) { throw '无法唯一确认旧 Flow 监听进程' }
        $legacyProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($owners[0])" -ErrorAction Stop
        $knownLegacy = $Health.runtime -ne 'qqbot-ai-browser' -and
            $legacyProcess.CommandLine -match '(?i)(?:^|[\s"\\/])src[\\/]api-server\.js(?:[\s"]|$)'
        $knownAi = $false
        if ($Health.runtime -eq 'qqbot-ai-browser' -and $scriptPath) {
            $taskAiEntry = Join-Path (Split-Path -Parent (Split-Path -Parent $scriptPath)) 'scripts\flow-ai\api.mjs'
            $knownAi = $legacyProcess.CommandLine -match ('(?i)(?:^|[\s"])' + [regex]::Escape($taskAiEntry) + '(?:[\s"]|$)')
        }
        if ($legacyProcess.Name -notmatch '^node(?:\.exe)?$' -or -not ($knownLegacy -or $knownAi)) {
            throw '监听进程不是已识别的旧 Flow Node 服务'
        }
        $latest = Get-FlowHealth
        if (-not $latest -or $latest.ok -ne $true -or $latest.runtime -ne $Health.runtime -or
            -not $latest.pool -or $null -eq $latest.pool.busy_workers -or
            $null -eq $latest.pool.queued_attempts -or [int]$latest.pool.busy_workers -gt 0 -or
            [int]$latest.pool.queued_attempts -gt 0 -or
            ($latest.runtime -eq 'qqbot-ai-browser' -and (-not $knownAi -or [int]$latest.runtime_version -ge 5 -or
             -not $latest.browser_resources -or $null -eq $latest.browser_resources.active -or
             $null -eq $latest.browser_resources.queued -or [int]$latest.browser_resources.active -gt 0 -or
             [int]$latest.browser_resources.queued -gt 0))) {
            throw '迁移前空闲复查未通过'
        }
        $taskLatestOwners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
        if ($taskLatestOwners.Count -ne 1 -or $taskLatestOwners[0] -ne $legacyProcess.ProcessId) {
            throw '迁移前监听进程已经变化，不停止任何进程'
        }
        Stop-Process -Id $legacyProcess.ProcessId -ErrorAction Stop
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if (-not (Test-FlowPortInUse)) {
                Write-FlowLog '已停止空闲旧 Flow 服务，正在加载新版 AI 浏览器任务服务。' ([ConsoleColor]::Green)
                return $true
            }
            Start-Sleep -Milliseconds 100
        }
        throw '旧 Flow 服务端口尚未释放'
    } catch {
        Write-FlowLog "旧 Flow 服务未迁移：$($_.Exception.Message)" ([ConsoleColor]::Yellow)
        return $false
    }
}

function Test-FlowPortInUse {
    try {
        $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        return ($listeners.Count -gt 0)
    } catch {
        return $false
    }
}

function Start-FlowApi {
    # Configure only profile paths; missing authentication requires a normal
    # browser login, not an exported cookie or storage-state injection.
    if (-not (Ensure-FlowSession)) { return $null }
    [void](Ensure-FlowBrowserExecutable)

    if (Test-FlowHealth) {
        Write-FlowLog 'Flow 按需服务已在线；启动和健康查询不会预加载 Chrome，浏览器全局最多 1 个。'
        return $null
    }

    $existingHealth = Get-FlowHealth
    if ($existingHealth -and $existingHealth.ok -eq $true) {
        if ($existingHealth.runtime -eq 'qqbot-ai-browser' -and [int]$existingHealth.runtime_version -ge 5) {
            Write-FlowLog '现有 Flow 服务未满足新版按需/单浏览器资源约束；未自动结束其任务，请关闭旧 Flow 服务及守护后重新启动。' ([ConsoleColor]::Yellow)
            return $null
        }
        if (-not (Remove-IdleLegacyFlowApi -Health $existingHealth)) { return $null }
    }

    if (Test-FlowPortInUse) {
        Write-FlowLog "127.0.0.1:$Port 已被其他程序占用，暂不覆盖该进程；请检查 FLOW_API_PORT。" ([ConsoleColor]::Yellow)
        return $null
    }

    $project = Get-FlowProject
    if (-not $project) {
        Write-FlowLog '未找到 Google Flow API 项目。请设置 FLOW_VIDEO_PROJECT_DIR，守护会继续重试。' ([ConsoleColor]::Yellow)
        return $null
    }

    $node = Get-NodePath
    if (-not $node) {
        Write-FlowLog '未找到 Node.js。请安装 Node.js 20+ 或设置 FLOW_NODE_PATH，守护会继续重试。' ([ConsoleColor]::Yellow)
        return $null
    }

    $logDirectory = $null
    if ($script:LogPath) {
        $logDirectory = Split-Path -Parent $script:LogPath
    } else {
        $logDirectory = Join-Path $project 'logs'
        try { New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null } catch {}
    }
    $stdoutPath = Join-Path $logDirectory 'flow-api.stdout.log'
    $stderrPath = Join-Path $logDirectory 'flow-api.stderr.log'

    # These values are inherited by the child process; FLOW_API_KEY is preserved.
    $env:FLOW_API_HOST = '127.0.0.1'
    $env:FLOW_API_PORT = "$Port"
    $repositoryRoot = Split-Path -Parent (Split-Path -Parent $scriptPath)
    $aiEntrypoint = Join-Path $repositoryRoot 'scripts\flow-ai\api.mjs'
    if (-not (Test-Path -LiteralPath $aiEntrypoint -PathType Leaf)) {
        throw "QQBot AI Flow 浏览器任务入口不存在：$aiEntrypoint"
    }
    $env:FLOW_VIDEO_PROJECT_DIR = $project
    if ($DataDir) { $env:QQ_AI_DATA_DIR = $DataDir }
    $pythonCandidate = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $pythonCandidate -PathType Leaf) {
        $env:FLOW_AI_PYTHON = $pythonCandidate
    }
    $env:FLOW_POLL_MS = '5000'
    # Queue time is not counted. Once a worker has submitted the request, allow
    # up to 20 minutes for Flow to return and download the generated video.
    $env:FLOW_TIMEOUT_MS = '1200000'
    if (-not $env:FLOW_MAX_CREDITS) {
        # Match the Flow companion's documented Pro budget.  This is only a
        # local preflight ledger; Google Flow remains the authoritative quota.
        $env:FLOW_MAX_CREDITS = '1050'
    }

    try {
        $process = Start-Process -FilePath $node -ArgumentList @(('"{0}"' -f $aiEntrypoint)) -WorkingDirectory $repositoryRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        Write-FlowLog "已启动 QQBot AI Flow 按需服务（PID $($process.Id)，端口 $Port），正在检查服务健康；不会打开 Chrome，尚未进行账号认证检测。日志：$stderrPath" ([ConsoleColor]::Green)
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            if (Test-FlowHealth) {
                Write-FlowLog 'Flow 按需服务已在线；未启动任何浏览器，生成或人工登录时才打开，浏览器全局最多 1 个。'
                break
            }
            if ($process.HasExited) {
                break
            }
            Start-Sleep -Seconds 1
        }
        return $process
    } catch {
        Write-FlowLog "启动 Google Flow API 失败：$($_.Exception.Message)" ([ConsoleColor]::Red)
        return $null
    }
}

function Get-GuardProcesses {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.ProcessId -ne $PID -and
            $_.CommandLine -and
            $_.CommandLine -match '(?i)flow-api-guard\.ps1' -and
            $_.CommandLine -match '(?i)(?:-|/)Guard(?:\s|$)'
        })
    } catch {
        return @()
    }
}

function Start-Guard {
    if (@(Get-GuardProcesses).Count -gt 0) {
        Write-FlowLog 'Google Flow API 守护进程已存在，不重复启动。'
        return
    }

    $powershell = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if (-not $powershell) {
        $powershell = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    }
    if (-not $powershell) {
        Write-FlowLog '未找到 powershell.exe/pwsh.exe，无法启动 Flow 守护进程。' ([ConsoleColor]::Yellow)
        return
    }

    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden',
        '-File', ('"{0}"' -f $scriptPath),
        '-Guard',
        '-Port', "$Port",
        '-PollSeconds', "$PollSeconds"
    )
    if ($FlowProjectDir) {
        $arguments += '-FlowProjectDir'
        $arguments += ('"{0}"' -f $FlowProjectDir)
    }
    if ($DataDir) {
        $arguments += '-DataDir'
        $arguments += ('"{0}"' -f $DataDir)
    }

    try {
        $child = Start-Process -FilePath $powershell.Source -ArgumentList $arguments -WindowStyle Hidden -PassThru
        Write-FlowLog "已启动 Flow API 守护进程（PID $($child.Id)）。"
    } catch {
        Write-FlowLog "启动 Flow API 守护进程失败：$($_.Exception.Message)" ([ConsoleColor]::Red)
    }
}

function Invoke-GuardLoop {
    # A named mutex closes the small race where two start commands run at the
    # same time and both try to become the watchdog.
    $mutex = $null
    $ownsMutex = $false
    try {
        $mutex = New-Object System.Threading.Mutex($false, 'Global\QQAIGroupAgentFlowGuard')
        $ownsMutex = $mutex.WaitOne(0)
        if (-not $ownsMutex) {
            return
        }
    } catch {
        # Mutex creation can be denied in unusual service contexts; the process
        # scan still prevents normal duplicate starts.
    }

    $childProcess = $null
    try {
        while ($true) {
            if (Test-FlowHealth) {
                Start-Sleep -Seconds $PollSeconds
                continue
            }

            if ($childProcess -and -not $childProcess.HasExited) {
                Start-Sleep -Seconds ([Math]::Max(2, $PollSeconds))
                continue
            }
            $childProcess = $null

            $childProcess = Start-FlowApi
            if ($childProcess) {
                # The Node server normally binds immediately.  Wait briefly so
                # a slow startup is not mistaken for a crashed child.
                for ($attempt = 0; $attempt -lt 10; $attempt++) {
                    if (Test-FlowHealth) {
                        break
                    }
                    if ($childProcess.HasExited) {
                        break
                    }
                    Start-Sleep -Seconds 1
                }
            } else {
                Start-Sleep -Seconds ([Math]::Max(15, $PollSeconds))
            }
        }
    } finally {
        if ($mutex -and $ownsMutex) {
            try { $mutex.ReleaseMutex() | Out-Null } catch {}
        }
        if ($mutex) {
            $mutex.Dispose()
        }
    }
}

Initialize-LogPath

if ($Guard) {
    Invoke-GuardLoop
    exit 0
}

$started = Start-FlowApi
if ($Once) {
    if ($started) {
        Write-FlowLog "Flow API 已启动；使用健康检查确认 http://127.0.0.1:$Port/healthz。"
    }
    exit 0
}

if ($started) {
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        if (Test-FlowHealth) {
            break
        }
        if ($started.HasExited) {
            break
        }
        Start-Sleep -Seconds 1
    }
}
Start-Guard
exit 0
