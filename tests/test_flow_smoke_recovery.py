"""Exercise the recovery command without a service, browser or paid request."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from app.flow_video import FlowVideoClient


@pytest.mark.asyncio
async def test_smoke_resume_uses_existing_http_method_and_never_creates(monkeypatch, capsys, tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "flow-ai-smoke.py"
    spec = importlib.util.spec_from_file_location("flow_smoke_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    client = FlowVideoClient()

    async def get(job_id):
        calls.append(("get", job_id))
        if len(calls) == 1:
            return {"status": "failed"}
        return {"status": "in_progress", "submission_confirmed": True, "seconds": "8",
                "size": "1280x720", "created_at": 1, "started_at": 2, "project_url": "https://flow.google.com/project/original"}

    async def request_json(method, path, **kwargs):
        calls.append((method, path))
        return {"status": "in_progress"}

    monkeypatch.setattr(client, "get", get)
    monkeypatch.setattr(client, "_request_json", request_json)

    class FakeDB:
        def create_turn(self, *args): return 1
        def get_flow_video_job(self, *args): return None
        def create_flow_video_job(self, conversation, job_id, **kwargs):
            calls.append(("store", conversation, job_id, kwargs["project_url"]))
        def update_flow_video_job(self, *args, **kwargs): pass
        def finish_turn(self, *args, **kwargs): pass

    class FakeService:
        def __init__(self, data_dir): self.db = FakeDB()
        def _flow_video_client(self): return client
        def _flow_browser_state(self, upstream): return {"submission_confirmed": upstream["submission_confirmed"]}
        async def _execute_tool(self, *args, **kwargs): pytest.fail("recovery cannot create a generation")
        async def stop(self): calls.append(("stop",))

    monkeypatch.setattr(module, "AgentService", FakeService)
    await module.main(argparse.Namespace(data_dir=tmp_path, conversation="private:42", resume_job="video_original", prompt="unused"))
    assert ("POST", "/v1/videos/video_original/resume-review") in calls
    assert ("store", "private:42", "video_original", "https://flow.google.com/project/original") in calls
    assert calls[-1] == ("stop",)
    result = json.loads(capsys.readouterr().out)
    assert result["recovered_without_generation"] is True
    assert result["job_id"] == "video_original"
