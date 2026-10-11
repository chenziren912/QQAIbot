"""Non-paid checks for runtime migration and charged-job recovery boundaries."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

from app.service import AgentService


def test_flow_guard_requires_single_lazy_browser_runtime() -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell unavailable")
    script = Path(__file__).resolve().parents[1] / "scripts" / "flow-api-guard.ps1"
    escaped = str(script).replace("'", "''")
    harness = f"""
$taskTokens=$null; $taskErrors=$null
$taskAst=[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$taskTokens,[ref]$taskErrors)
if ($taskErrors.Count) {{ throw ($taskErrors | Out-String) }}
foreach ($taskStatement in $taskAst.EndBlock.Statements) {{
    if ($taskStatement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $taskStatement.Name -eq 'Test-FlowHealth') {{
        . ([scriptblock]::Create($taskStatement.Extent.Text))
    }}
}}
function Get-FlowHealth {{ return $script:taskHealth }}
$script:taskHealth=@{{ok=$true;runtime='qqbot-ai-browser';runtime_version=5;browser_resources=@{{limit=1;lazy_start=$true;release_after_task=$true}};authentication_mode='persistent_browser_profile';cookie_import_enabled=$false;storage_state_import_enabled=$false;ai_browser_control=$true;submission_confirmation='flow_page_dom'}}
$taskResults=@((Test-FlowHealth))
$script:taskHealth.runtime_version=4; $taskResults+=(Test-FlowHealth)
$script:taskHealth.runtime_version=5; $script:taskHealth.browser_resources.limit=5; $taskResults+=(Test-FlowHealth)
$script:taskHealth.browser_resources.limit=1; $script:taskHealth.browser_resources.lazy_start=$false; $taskResults+=(Test-FlowHealth)
$script:taskHealth.browser_resources.lazy_start=$true; $script:taskHealth.browser_resources.release_after_task=$false; $taskResults+=(Test-FlowHealth)
$taskResults | ConvertTo-Json -Compress
"""
    result = subprocess.run([powershell, "-NoProfile", "-Command", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == [True, False, False, False, False]


@pytest.mark.parametrize("account", [1, 3, 4, 5])
def test_flow_login_matches_real_profile_login_response(account: int) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell unavailable")
    script = Path(__file__).resolve().parents[1] / "scripts" / "flow-login.ps1"
    harness = r"""
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec, $Method)
    if ($Uri -match '/healthz$') { return @{ok=$true;pool=@{workers=@(@{account=ACCOUNT;active=$false;queued=0;stage='idle'})}} }
    if ($Uri -match '/login\?') { return @{account=ACCOUNT;opened=$true;persistentProfile=$true;requiresUserConfirmation=$true} }
    if ($Uri -match '/login/verify$') { return @{account=ACCOUNT;ready=$true;persistentProfile=$true;url='https://flow.google.com/'} }
    throw 'Unexpected request: login script must not cancel a valid profile login'
}
function Read-Host { param($Prompt) return '' }
& 'SCRIPT_PATH' -Account ACCOUNT
""".replace("SCRIPT_PATH", str(script).replace("'", "''")).replace("ACCOUNT", str(account))
    result = subprocess.run([powershell, "-NoProfile", "-Command", harness], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "登录修复失败" not in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    {"submission_confirmed": True}, {"submission_unknown": True},
    {"browser_task": {"id": "submit_video_test", "status": "completed"}},
    {"review_task": {"id": "review_video_test", "status": "running"}},
])
async def test_ai_job_timeout_never_creates_replacement(tmp_path: Path, state: dict) -> None:
    service = AgentService(tmp_path / "data")
    service._flow_video_client = lambda: pytest.fail("must not contact a second charged generation")
    try:
        job = {"conversation_id": "test", "job_id": "video_test", "prompt": "test", "browser_state": state}
        assert await service._retry_flow_video_job_once(
            job, "Timed out after 1200 seconds waiting for Google Flow."
        ) == (None, "")
    finally:
        await service.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_runtime,confirmed", [(True, True), (False, False), (False, True)])
async def test_legacy_retry_requires_ai_runtime_workspace_and_confirmation(tmp_path: Path, old_runtime: bool, confirmed: bool) -> None:
    service = AgentService(tmp_path / "data")
    calls = []

    class Client:
        async def health(self):
            return {"ok": True, "runtime": "legacy" if old_runtime else "qqbot-ai-browser",
                    "ai_browser_control": True, "submission_confirmation": "flow_page_dom"}

        async def create(self, **kwargs):
            calls.append(kwargs)
            return {"id": "video_new", "status": "in_progress", "submission_confirmed": confirmed,
                    "browser_task": {"id": "submit_video_new", "status": "completed"}}

    service._flow_video_client = lambda: Client()
    try:
        job = {"conversation_id": "test", "job_id": "video_old", "prompt": "test"}
        replacement, error = await service._retry_flow_video_job_once(
            job, "Timed out after 1200 seconds waiting for Google Flow."
        )
        if old_runtime:
            assert not calls and replacement is None and "未再次提交" in error
        else:
            assert calls[0]["workspace_dir"] == str(service.conversation_workspace("test"))
            if confirmed:
                assert replacement["browser_state"]["submission_confirmed"] is True
                assert replacement["browser_state"]["browser_task"]["id"] == "submit_video_new"
                assert not error
            else:
                assert replacement is None and "未确认" in error
    finally:
        await service.stop()


def test_flow_guard_migrates_only_identified_idle_legacy_process() -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required for guard AST/unit checks")
    guard = Path(__file__).resolve().parents[1] / "scripts" / "flow-api-guard.ps1"
    escaped = str(guard).replace("'", "''")
    harness = f"""
$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
$names = @('Test-FlowHealth', 'Remove-IdleLegacyFlowApi')
foreach ($statement in $ast.EndBlock.Statements) {{
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $statement.Name -in $names) {{
        . ([scriptblock]::Create($statement.Extent.Text))
    }}
}}
$Port = 8787
$script:stopped = 0
$script:mockHealth = [pscustomobject]@{{ok=$true;pool=[pscustomobject]@{{busy_workers=0;queued_attempts=0}}}}
function Get-FlowHealth {{ return $script:mockHealth }}
function Get-FlowHeaders {{ return @{{}} }}
function Write-FlowLog {{ param($Message,$Color) }}
$script:pending = $false
function Invoke-RestMethod {{
    if ($script:pending) {{ return [pscustomobject]@{{data=@([pscustomobject]@{{status='in_progress'}})}} }}
    return [pscustomobject]@{{data=@()}}
}}
function Get-NetTCPConnection {{ return [pscustomobject]@{{OwningProcess=12345}} }}
function Get-CimInstance {{ return [pscustomobject]@{{Name='node.exe';CommandLine='node src/api-server.js';ProcessId=12345}} }}
function Stop-Process {{ param($Id); $script:stopped++ }}
function Test-FlowPortInUse {{ return $false }}
$legacyHealthy = Test-FlowHealth
$script:mockHealth.pool.busy_workers = 1
$busy = Remove-IdleLegacyFlowApi -Health $script:mockHealth
$busyStopped = $script:stopped
$script:mockHealth.pool.busy_workers = 0
$script:pending = $true
$pending = Remove-IdleLegacyFlowApi -Health $script:mockHealth
$pendingStopped = $script:stopped
$script:pending = $false
$idle = Remove-IdleLegacyFlowApi -Health $script:mockHealth
$idleStopped = $script:stopped
function Get-CimInstance {{ return [pscustomobject]@{{Name='python.exe';CommandLine='uvicorn app.main:app';ProcessId=12345}} }}
$other = Remove-IdleLegacyFlowApi -Health $script:mockHealth
$script:mockHealth | Add-Member -NotePropertyName runtime -NotePropertyValue 'qqbot-ai-browser'
$script:mockHealth | Add-Member -NotePropertyName authentication_mode -NotePropertyValue 'persistent_browser_profile'
$script:mockHealth | Add-Member -NotePropertyName cookie_import_enabled -NotePropertyValue $false
$script:mockHealth | Add-Member -NotePropertyName storage_state_import_enabled -NotePropertyValue $false
$script:mockHealth | Add-Member -NotePropertyName ai_browser_control -NotePropertyValue $true
$script:mockHealth | Add-Member -NotePropertyName runtime_version -NotePropertyValue 5
$script:mockHealth | Add-Member -NotePropertyName browser_resources -NotePropertyValue @{{limit=1;lazy_start=$true;release_after_task=$true}}
$script:mockHealth | Add-Member -NotePropertyName submission_confirmation -NotePropertyValue 'flow_page_dom'
$aiHealthy = Test-FlowHealth
[pscustomobject]@{{legacyHealthy=$legacyHealthy;busy=$busy;busyStopped=$busyStopped;pending=$pending;pendingStopped=$pendingStopped;idle=$idle;idleStopped=$idleStopped;other=$other;totalStopped=$script:stopped;aiHealthy=$aiHealthy}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run([shell, "-NoProfile", "-Command", harness], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    assert result == {"legacyHealthy": False, "busy": False, "busyStopped": 0,
                      "pending": False, "pendingStopped": 0,
                      "idle": True, "idleStopped": 1, "other": False,
                      "totalStopped": 1, "aiHealthy": True}


@pytest.mark.parametrize("account_count", [2, 5])
def test_flow_guard_uses_profiles_without_loading_legacy_exports(tmp_path: Path, account_count: int) -> None:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell is required")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    guard = scripts / "flow-api-guard.ps1"
    for entry in ["start.ps1", "console.ps1", "install-autostart.ps1", "env/start.ps1", "env/console.ps1"]:
        assert "FlowCookiesFile" not in (scripts / entry).read_text(encoding="utf-8")
    source = guard.read_text(encoding="utf-8")
    assert "Get-FlowCookieCandidates" not in source
    assert "import-netscape-cookies" not in source
    escaped = str(guard).replace("'", "''")
    directory = str(tmp_path).replace("'", "''")
    harness = f"""
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$tokens,[ref]$errors)
if ($errors.Count) {{ throw ($errors | Out-String) }}
foreach ($statement in $ast.EndBlock.Statements) {{
    if ($statement -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $statement.Name -in @('Import-UserFlowSessionEnvironment','Get-SessionDataDirectory','Ensure-FlowProfileRoot','Ensure-FlowSession')) {{
        . ([scriptblock]::Create($statement.Extent.Text))
    }}
}}
function Write-FlowLog {{ param($Message,$Color) }}
function Import-UserFlowSessionEnvironment {{ }}
foreach ($entry in @(Get-ChildItem Env:)) {{ if ($entry.Name -match '^FLOW_USER_DATA_DIR') {{ Remove-Item -LiteralPath ('Env:' + $entry.Name) }} }}
$DataDir='{directory}'
$env:FLOW_ACCOUNT_COUNT='{account_count}'
$env:FLOW_PROFILE_ROOT=Join-Path $DataDir 'profiles'
$env:FLOW_USER_DATA_DIR=Join-Path $env:FLOW_PROFILE_ROOT 'account-1'
$env:FLOW_USER_DATA_DIR_2=Join-Path $env:FLOW_PROFILE_ROOT 'account-2'
$env:FLOW_PERSISTENT_PROFILES='0'
$env:FLOW_COOKIES_FILE='missing-file-that-must-not-be-read'
$env:FLOW_STORAGE_STATE_2='missing-state-that-must-not-be-read'
$ok=Ensure-FlowSession
[pscustomobject]@{{ok=$ok;persistent=$env:FLOW_PERSISTENT_PROFILES;cookie=$env:FLOW_COOKIES_FILE;state=$env:FLOW_STORAGE_STATE_2;first=(Test-Path -LiteralPath $env:FLOW_USER_DATA_DIR);second=(Test-Path -LiteralPath $env:FLOW_USER_DATA_DIR_2);count=[int]$env:FLOW_ACCOUNT_COUNT;directories=@($env:FLOW_USER_DATA_DIRS -split ';').Count;last=(Test-Path -LiteralPath (Join-Path $env:FLOW_PROFILE_ROOT 'account-{account_count}'))}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run([shell, "-NoProfile", "-Command", harness], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip())
    # Windows PowerShell and pwsh serialize an unset environment variable
    # differently (empty string vs null); neither may retain an export path.
    assert not result.pop("cookie")
    assert not result.pop("state")
    assert result == {"ok": True, "persistent": "1", "first": True, "second": True, "count": account_count, "directories": account_count, "last": True}
