"""A scheduling upgrade must never stop a paid task or another listener."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


def test_guard_upgrades_only_known_idle_outdated_ai_runtime():
    shell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell unavailable")
    script = Path(__file__).resolve().parents[1] / "scripts" / "flow-api-guard.ps1"
    escaped = str(script).replace("'", "''")
    harness = rf"""
$scriptPath='{escaped}'
$taskErrors=$null; $taskTokens=$null
$taskAst=[System.Management.Automation.Language.Parser]::ParseFile($scriptPath,[ref]$taskTokens,[ref]$taskErrors)
if ($taskErrors.Count) {{ throw ($taskErrors | Out-String) }}
foreach ($taskStatement in $taskAst.EndBlock.Statements) {{
 if ($taskStatement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $taskStatement.Name -eq 'Remove-IdleLegacyFlowApi') {{
  . ([scriptblock]::Create($taskStatement.Extent.Text))
 }}
}}
$Port=8787
$taskEntry=Join-Path (Split-Path -Parent (Split-Path -Parent $scriptPath)) 'scripts\flow-ai\api.mjs'
$script:taskOwner='node "'+$taskEntry+'"'
$script:taskStopped=0; $script:taskPending=$false; $script:taskFreshMode=''; $script:taskOwnerQueries=0
$script:taskHealth=[pscustomobject]@{{ok=$true;runtime='qqbot-ai-browser';runtime_version=4;pool=@{{busy_workers=0;queued_attempts=0}};browser_resources=@{{active=0;queued=0}}}}
function Get-FlowHealth {{
 $taskCopy=$script:taskHealth | ConvertTo-Json -Depth 5 | ConvertFrom-Json
 if ($script:taskFreshMode -eq 'bad_ok') {{ $taskCopy.ok=$false }}
 if ($script:taskFreshMode -eq 'new_runtime') {{ $taskCopy.runtime='unidentified-listener' }}
 return $taskCopy
}}
function Get-FlowHeaders {{ return @{{}} }}
function Write-FlowLog {{ param($Message,$Color) }}
function Get-NetTCPConnection {{
 $script:taskOwnerQueries++
 if ($script:taskFreshMode -eq 'new_owner' -and $script:taskOwnerQueries -gt 1) {{ return [pscustomobject]@{{OwningProcess=54321}} }}
 return [pscustomobject]@{{OwningProcess=12345}}
}}
function Get-CimInstance {{ return [pscustomobject]@{{Name='node.exe';CommandLine=$script:taskOwner;ProcessId=12345}} }}
function Stop-Process {{ param($Id); if ($Id -ne 12345) {{ throw 'wrong process' }}; $script:taskStopped++ }}
function Test-FlowPortInUse {{ return $false }}
function Invoke-RestMethod {{
 if ($script:taskPending) {{ return [pscustomobject]@{{data=@([pscustomobject]@{{status='queued'}})}} }}
 return [pscustomobject]@{{data=@()}}
}}
$taskResults=@()
$script:taskHealth.pool.busy_workers=1
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskHealth.pool.busy_workers=0; $script:taskHealth.browser_resources.active=1
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskHealth.browser_resources.active=0; $script:taskHealth.browser_resources.queued=1
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskHealth.browser_resources.queued=0; $script:taskPending=$true
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskPending=$false; $script:taskOwner='node "D:\other-project\scripts\flow-ai\api.mjs"'
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskOwner='node "'+$taskEntry+'"'; $script:taskHealth.runtime_version=5
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
$script:taskHealth.runtime_version=4
foreach ($taskMode in @('bad_ok','new_runtime','new_owner')) {{
 $script:taskFreshMode=$taskMode; $script:taskOwnerQueries=0
 $taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
}}
$script:taskFreshMode=''; $script:taskOwnerQueries=0
$taskBefore=$script:taskStopped
$taskResults+=(Remove-IdleLegacyFlowApi -Health $script:taskHealth)
[pscustomobject]@{{results=$taskResults;before=$taskBefore;stopped=$script:taskStopped}} | ConvertTo-Json -Compress
"""
    result = subprocess.run([shell, "-NoProfile", "-Command", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout.strip())
    assert data == {"results": [False] * 9 + [True], "before": 0, "stopped": 1}


def test_start_path_does_not_skip_outdated_ai_migration():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "flow-api-guard.ps1").read_text(encoding="utf-8")
    start = source.split("function Start-FlowApi {", 1)[1].split("function Get-GuardProcesses", 1)[0]
    assert "if ($existingHealth.runtime -eq 'qqbot-ai-browser' -and [int]$existingHealth.runtime_version -ge 5)" in start
    assert "Remove-IdleLegacyFlowApi -Health $existingHealth" in start
