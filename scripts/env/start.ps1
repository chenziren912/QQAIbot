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
    [string]$FlowCookiesFile,
    [string]$FlowCookiesFile2,
    [switch]$NoFlowApi,
    [switch]$KeepOpenOnError,
    [switch]$Console
)

# `scripts\env` is kept as a compatibility entry point for older packages.
# Keep all startup behavior in the canonical root script so this copy cannot
# silently miss the Flow API watchdog or newer Python/Node diagnostics.
$ErrorActionPreference = 'Stop'
$rootScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'start.ps1'
if (-not (Test-Path -LiteralPath $rootScript -PathType Leaf)) {
    throw "找不到主启动脚本：$rootScript"
}

& $rootScript @PSBoundParameters
exit $LASTEXITCODE
