"""Submit ONE real Flow video through the Bot's tool executor.

Uses the live database without starting a second QQ connection/agent worker.
The already-running Bot observes the persisted job and delivers its result.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.service import AgentService


async def main(args: argparse.Namespace) -> None:
    service = AgentService(args.data_dir)
    turn_id = service.db.create_turn(args.conversation, [])
    try:
        if args.resume_job:
            client = service._flow_video_client()
            existing = await client.get(args.resume_job)
            if existing.get("status") == "failed":
                await client._request_json("POST", f"/v1/videos/{args.resume_job}/resume-review")
            elif not existing.get("submission_confirmed") and not (existing.get("submission_unknown") and existing.get("status") == "queued"):
                raise RuntimeError("该任务不是可恢复的已点击生成任务")
            while True:
                upstream = await client.get(args.resume_job)
                if upstream.get("submission_confirmed"):
                    break
                if upstream.get("status") == "failed":
                    raise RuntimeError(str(upstream.get("error")))
                await asyncio.sleep(6)
            if not service.db.get_flow_video_job(args.conversation, args.resume_job):
                service.db.create_flow_video_job(
                    args.conversation, args.resume_job, prompt=upstream.get("prompt") or args.prompt,
                    model="gemini-omni-flash", seconds=int(upstream["seconds"]), size=upstream["size"], outputs=1,
                    status=upstream["status"], progress=int(upstream.get("progress") or 0),
                    created_at=str(upstream["created_at"]), started_at=str(upstream.get("started_at") or ""),
                    project_url=upstream["project_url"],
                )
            service.db.update_flow_video_job(args.conversation, args.resume_job, browser_state=service._flow_browser_state(upstream))
            result = {"ok": True, "job_id": args.resume_job, "recovered_without_generation": True, "submission_confirmed": True}
        else:
            result = await service._execute_tool(
                turn_id, args.conversation, "Builtin_video_generation",
                {"action": "create", "prompt": args.prompt, "seconds": 8, "n": 1, "model": "gemini-omni-flash"},
                "real-flow-ai-smoke",
            )
        service.db.finish_turn(turn_id, "completed" if result.get("ok") else "failed", error=result.get("error", ""))
        print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        await service.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="通过 Bot 工具真实提交一次 Flow 视频测试（消耗 Flow 额度）")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--resume-job", help="只恢复已点击生成的原项目审查；不再次生成或扣额度")
    parser.add_argument("--prompt", default="A tiny orange cat waves one paw at the camera in a cozy sunlit living room. One continuous shot, gentle motion, no text. This is a short video test.")
    asyncio.run(main(parser.parse_args()))
