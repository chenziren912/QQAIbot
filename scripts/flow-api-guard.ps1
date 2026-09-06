[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8787,
    [string]$FlowProjectDir,
    [string]$FlowCookiesFile,
    [string]$FlowCookiesFile2,
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
$script:FlowSessionConfigured = $false

function Import-UserFlowSessionEnvironment {
    # A shell that was already open when the user added the second account does
    # not automatically receive new User environment variables.  Pull only the
    # session path variables into this process so an immediate restart works;
    # cookie contents never pass through the command line or logs.
    foreach ($name in @(
        'FLOW_STORAGE_STATE', 'FLOW_STORAGE_STATE_2', 'FLOW_STORAGE_STATES',
        'FLOW_COOKIES_FILE', 'FLOW_COOKIES_FILE_2', 'FLOW_COOKIES_FILES',
        'FLOW_PROFILE_ROOT', 'FLOW_USER_DATA_DIR', 'FLOW_USER_DATA_DIR_2',
        'FLOW_USER_DATA_DIRS', 'FLOW_PERSISTENT_PROFILES'
    )) {
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

function Get-FlowCookieCandidates {
    param([ValidateSet(1, 2)][int]$Account = 1)

    $candidates = @()
    $explicit = if ($Account -eq 1) { $FlowCookiesFile } else { $FlowCookiesFile2 }
    $environmentName = if ($Account -eq 1) { 'FLOW_COOKIES_FILE' } else { 'FLOW_COOKIES_FILE_2' }
    if ($explicit) {
        $candidates += $explicit
    }
    $environmentValue = [Environment]::GetEnvironmentVariable($environmentName, 'Process')
    if ($environmentValue) {
        $candidates += $environmentValue
    }
    if ($env:FLOW_COOKIES_FILES) {
        $plural = @($env:FLOW_COOKIES_FILES -split ';' | Where-Object { $_ })
        if ($plural.Count -ge $Account) {
            $candidates += $plural[$Account - 1]
        }
    }

    if ($env:USERPROFILE) {
        $downloads = Join-Path $env:USERPROFILE 'Downloads'
        if (Test-Path -LiteralPath $downloads -PathType Container) {
            # The Codex Google Flow skill keeps its converted cookie file
            # here.  Also accept Netscape exports copied from Chrome.  When
            # two exports are present, account 2 receives the newest export
            # that is different from the source selected for account 1.
            if ($Account -eq 1) {
                $candidates += (Join-Path $env:USERPROFILE '.codex\skills\google-flow-video\mcp\secrets\flow-cookies.json')
            }
            $netscapeFiles = @(Get-ChildItem -LiteralPath $downloads -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '(?i)^labs\.google_cookies(?: \(\d+\))?\.txt$' } |
                Sort-Object LastWriteTimeUtc -Descending)
            $accountOneSource = [Environment]::GetEnvironmentVariable('FLOW_COOKIES_FILE', 'Process')
            $resolvedAccountOneSource = $accountOneSource
            if ($accountOneSource) {
                try { $resolvedAccountOneSource = (Resolve-Path -LiteralPath $accountOneSource -ErrorAction Stop).Path } catch {}
            }
            foreach ($file in $netscapeFiles) {
                if ($Account -eq 2 -and $resolvedAccountOneSource -and
                    $resolvedAccountOneSource -eq $file.FullName) {
                    continue
                }
                $candidates += $file.FullName
            }
            if ($Account -eq 2) {
                # Keep the historical JSON fallback for users who explicitly
                # exported a Playwright/extension cookie file rather than a
                # Netscape file.
                Get-ChildItem -LiteralPath $downloads -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.Name -match '(?i)^(?:cookies|flow-cookies)(?: \(\d+\))?\.json$' } |
                    Sort-Object LastWriteTimeUtc -Descending |
                    ForEach-Object { $candidates += $_.FullName }
            }
        }
    }

    if ($Account -eq 1) {
        $project = Get-FlowProject
        if ($project) {
            $candidates += (Join-Path $project 'secrets\flow-cookies.json')
        }
    }
    return @($candidates | Where-Object { $_ } | Select-Object -Unique)
}

function Ensure-FlowSessionSource {
    param([ValidateSet(1, 2)][int]$Account)

    $storageName = if ($Account -eq 1) { 'FLOW_STORAGE_STATE' } else { 'FLOW_STORAGE_STATE_2' }
    $cookieName = if ($Account -eq 1) { 'FLOW_COOKIES_FILE' } else { 'FLOW_COOKIES_FILE_2' }
    $storage = [Environment]::GetEnvironmentVariable($storageName, 'Process')
    if ($storage) {
        if (Test-Path -LiteralPath $storage -PathType Leaf) {
            Write-FlowLog "Google Flow 账号 $Account 使用 storage-state 会话。" ([ConsoleColor]::Green)
            return $true
        }
        Write-FlowLog "账号 $Account 的 $storageName 文件不存在；尝试 Cookie 会话。" ([ConsoleColor]::Yellow)
        [Environment]::SetEnvironmentVariable($storageName, $null, 'Process')
    }

    $source = $null
    foreach ($candidate in (Get-FlowCookieCandidates -Account $Account)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $source = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
            break
        }
    }
    if (-not $source) {
        return $false
    }

    $extension = [IO.Path]::GetExtension($source).ToLowerInvariant()
    if ($extension -eq '.json') {
        [Environment]::SetEnvironmentVariable($cookieName, $source, 'Process')
        Write-FlowLog "已发现 Google Flow 账号 $Account 的 Cookie 配置。" ([ConsoleColor]::Green)
        return $true
    }

    if ($extension -ne '.txt') {
        Write-FlowLog "账号 $Account 的 Flow Cookie 文件格式不受支持；请使用 JSON 或 Netscape .txt。" ([ConsoleColor]::Yellow)
        return $false
    }

    $project = Get-FlowProject
    $node = Get-NodePath
    $converter = if ($project) { Join-Path $project 'scripts\import-netscape-cookies.mjs' } else { $null }
    if (-not $node -or -not $converter -or -not (Test-Path -LiteralPath $converter -PathType Leaf)) {
        Write-FlowLog "账号 $Account 找到 Netscape Cookie，但缺少 Node.js 或转换脚本；守护会继续重试。" ([ConsoleColor]::Yellow)
        return $false
    }

    $sessionDirectory = Get-SessionDataDirectory
    if (-not $sessionDirectory) {
        Write-FlowLog '无法创建 Flow Cookie 输出目录；请检查 QQ_AI_DATA_DIR。' ([ConsoleColor]::Yellow)
        return $false
    }
    $suffix = if ($Account -eq 1) { '' } else { '-2' }
    $destination = Join-Path $sessionDirectory "flow-cookies$suffix.json"
    $needsConversion = $true
    try {
        $destinationItem = Get-Item -LiteralPath $destination -ErrorAction Stop
        $sourceItem = Get-Item -LiteralPath $source -ErrorAction Stop
        $needsConversion = $sourceItem.LastWriteTimeUtc -gt $destinationItem.LastWriteTimeUtc -or $destinationItem.Length -lt 20
    } catch {
        $needsConversion = $true
    }

    if ($needsConversion) {
        try {
            & $node $converter $source $destination
            if ($LASTEXITCODE -ne 0) {
                throw "converter exit code $LASTEXITCODE"
            }
        } catch {
            Write-FlowLog "账号 $Account Netscape Cookie 转换失败：$($_.Exception.Message)" ([ConsoleColor]::Red)
            return $false
        }
    }

    [Environment]::SetEnvironmentVariable($cookieName, $destination, 'Process')
    Write-FlowLog "已将账号 $Account 的 Netscape Cookie 转换为本地配置。" ([ConsoleColor]::Green)
    return $true
}

function Ensure-FlowSession {
    Import-UserFlowSessionEnvironment
    if ($script:FlowSessionConfigured) {
        return $true
    }

    $configured = 0
    foreach ($account in 1, 2) {
        if (Ensure-FlowSessionSource -Account $account) {
            $configured++
        }
    }
    if ($configured -gt 0) {
        $script:FlowSessionConfigured = $true
        Write-FlowLog "已配置 $configured 个 Google Flow 会话账号；生成失败时最多轮换尝试 3 次。" ([ConsoleColor]::Green)
        return $true
    }
    return $false
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
    return ($response -and $response.ok -eq $true)
}

function Test-FlowReadiness {
    $response = Get-FlowHealth -Deep -TimeoutSeconds 90
    if (-not $response -or $response.generation_ready -ne $true) {
        $details = @()
        if ($response -and $response.accounts) {
            $details = @($response.accounts | ForEach-Object {
                if ($_.error) { "账号 $($_.account)：$($_.error)" }
            } | Where-Object { $_ })
        }
        $suffix = if ($details.Count) { ' ' + ($details -join ' | ') } else { ' 请查看 flow-api.stderr.log 中的账号诊断。' }
        Write-FlowLog ("Flow API 进程已启动，但严格可用性检测失败。" + $suffix) ([ConsoleColor]::Red)
        return $false
    }
    $usable = [int]$response.usable_account_count
    $total = [int]$response.session_account_count
    if ($response.full_concurrency_ready -eq $true) {
        Write-FlowLog "严格检测通过：$usable/$total 个账号可用，可并行生成；单任务等待上限 20 分钟。" ([ConsoleColor]::Green)
    } else {
        Write-FlowLog "严格检测部分通过：$usable/$total 个账号可用；当前只能按可用账号数量并行。" ([ConsoleColor]::Yellow)
    }
    return $true
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
    # Configure both browser sessions before launching Node so the child
    # inherits FLOW_STORAGE_STATE(_2)/FLOW_COOKIES_FILE(_2).  A missing session is still
    # non-fatal for the QQ service; the generated task will report the exact
    # setup error rather than blocking the main agent.
    [void](Ensure-FlowSession)
    [void](Ensure-FlowProfileRoot)
    [void](Ensure-FlowBrowserExecutable)

    if (Test-FlowHealth) {
        [void](Test-FlowReadiness)
        return $null
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

    # These values are inherited by the child process.  Existing credentials
    # (FLOW_API_KEY, FLOW_STORAGE_STATE(_2), FLOW_COOKIES_FILE(_2), etc.) are preserved.
    $env:FLOW_API_HOST = '127.0.0.1'
    $env:FLOW_API_PORT = "$Port"
    # Queue time is not counted. Once a worker has submitted the request, allow
    # up to 20 minutes for Flow to return and download the generated video.
    $env:FLOW_TIMEOUT_MS = '1200000'
    if (-not $env:FLOW_MAX_CREDITS) {
        # Match the Flow companion's documented Pro budget.  This is only a
        # local preflight ledger; Google Flow remains the authoritative quota.
        $env:FLOW_MAX_CREDITS = '1050'
    }

    try {
        $process = Start-Process -FilePath $node -ArgumentList @('src/api-server.js') -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
        Write-FlowLog "已启动 Google Flow API（PID $($process.Id)，端口 $Port），正在执行严格可用性检测。日志：$stderrPath" ([ConsoleColor]::Green)
        for ($attempt = 0; $attempt -lt 15; $attempt++) {
            if (Test-FlowHealth) {
                [void](Test-FlowReadiness)
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
    if ($FlowCookiesFile) {
        $arguments += '-FlowCookiesFile'
        $arguments += ('"{0}"' -f $FlowCookiesFile)
    }
    if ($FlowCookiesFile2) {
        $arguments += '-FlowCookiesFile2'
        $arguments += ('"{0}"' -f $FlowCookiesFile2)
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
