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
$rootScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'console.ps1'
if (-not (Test-Path -LiteralPath $rootScript -PathType Leaf)) {
    throw "找不到主终端控制台脚本：$rootScript"
}

& $rootScript @PSBoundParameters
exit $LASTEXITCODE
