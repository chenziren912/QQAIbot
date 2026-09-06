"""Small async client for the local Google Flow video API.

The Google Flow browser automation lives in a separate process (the
``google-flow-video`` skill).  This module deliberately knows only the local
OpenAI-compatible HTTP contract, so the QQ agent remains usable when Flow is
temporarily offline and can report the exact upstream error to the model.
"""

from __future__ import annotations

import json
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import httpx


class FlowVideoError(RuntimeError):
    """An error returned by, or while talking to, the Flow video endpoint."""

    def __init__(self, message: str, *, status_code: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status_code = int(status_code or 0)
        self.code = str(code or "")


FLOW_VIDEO_MODEL = "gemini-omni-flash"


def _error_text(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("detail") or "").strip()
            code = str(error.get("code") or "").strip()
            if message and code:
                return f"{message} (code={code})"
            if message:
                return message
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class FlowVideoClient:
    """HTTP client for the local Flow API, with bounded response diagnostics."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8787",
        api_key: str = "",
        *,
        timeout: float = 45.0,
        create_timeout: float = 180.0,
    ) -> None:
        value = str(base_url or "").strip().rstrip("/")
        if not value:
            value = "http://127.0.0.1:8787"
        # Accept both the documented root URL and a root ending in /v1.  The
        # Flow API routes themselves always include /v1, so avoid /v1/v1.
        self.base_url = value[:-3] if value.lower().endswith("/v1") else value
        self.api_key = str(api_key or "").strip()
        self.timeout = max(2.0, min(300.0, float(timeout)))
        # POST /v1/videos only waits for local input validation and background
        # task admission.  Keep a larger ceiling for an older companion API
        # that may still perform a strict Flow readiness check before it
        # returns; status polling and downloads retain the shorter timeout.
        self.create_timeout = max(2.0, min(300.0, float(create_timeout)))

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        request_timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        timeout = self.timeout if request_timeout is None else max(2.0, min(300.0, float(request_timeout)))
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                # A system proxy can turn a local Flow endpoint into an
                # opaque 503.  Remote URLs keep normal environment-proxy
                # behaviour, while loopback calls always stay local.
                trust_env=not _is_loopback_url(url),
            ) as client:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
        except httpx.TimeoutException as exc:
            raise FlowVideoError(f"Flow API 请求超时：{url}") from exc
        except httpx.HTTPError as exc:
            raise FlowVideoError(f"Flow API 无法连接：{url}；{exc}") from exc
        payload: Any
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = response.text[:4_000]
        if response.status_code < 200 or response.status_code >= 300:
            detail = _error_text(payload, f"HTTP {response.status_code}")
            raise FlowVideoError(
                f"Flow API 返回 HTTP {response.status_code}：{detail}",
                status_code=response.status_code,
                code=(payload.get("error", {}).get("code", "") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else ""),
            )
        if not isinstance(payload, dict):
            raise FlowVideoError("Flow API 返回的 JSON 不是对象")
        return payload

    async def create(
        self,
        *,
        prompt: str,
        model: str = FLOW_VIDEO_MODEL,
        seconds: int = 8,
        size: str = "1280x720",
        outputs: int = 1,
        input_reference: str = "",
        max_credits: Optional[int] = None,
    ) -> Dict[str, Any]:
        requested_model = str(model or FLOW_VIDEO_MODEL).strip()
        # Keep the HTTP adapter defensive even when it is called outside the
        # AgentService validation path.  The API and browser layers enforce the
        # same canonical model, so a direct caller cannot accidentally revive a
        # legacy Veo selection.
        model_key = requested_model.casefold().replace("–", "-").replace("—", "-")
        if model_key in {
            FLOW_VIDEO_MODEL,
            "gemini-omni-flash-1.1",
            "gemini-omni-flash-1_1",
            "gemini omni flash",
            "omni-1.1-flash",
            "omni 1.1 flash",
        }:
            requested_model = FLOW_VIDEO_MODEL
        else:
            raise FlowVideoError(
                "Flow 视频固定使用 Gemini Omni Flash 1.1（gemini-omni-flash），不支持其他模型",
                code="unsupported_model",
            )
        body: Dict[str, Any] = {
            "prompt": str(prompt),
            "model": requested_model,
            "seconds": int(seconds),
            "size": str(size or "1280x720"),
            "n": int(outputs),
        }
        if input_reference:
            body["input_reference"] = str(input_reference)
        if max_credits is not None:
            body["max_credits"] = int(max_credits)
        return await self._request_json(
            "POST",
            "/v1/videos",
            json_body=body,
            request_timeout=self.create_timeout,
        )

    async def health(self) -> Dict[str, Any]:
        """Return the companion API health payload for local diagnostics."""

        return await self._request_json("GET", "/healthz")

    async def get(self, job_id: str) -> Dict[str, Any]:
        safe_id = quote(str(job_id or "").strip(), safe="")
        if not safe_id:
            raise FlowVideoError("视频任务 ID 不能为空")
        return await self._request_json("GET", f"/v1/videos/{safe_id}")

    async def list(self, *, status: str = "", limit: int = 20) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
        if status:
            params["status"] = str(status)
        return await self._request_json("GET", "/v1/videos", params=params)

    async def download_content(
        self,
        job_id: str,
        destination: Path,
        *,
        index: int = 1,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> int:
        safe_id = quote(str(job_id or "").strip(), safe="")
        if not safe_id:
            raise FlowVideoError("视频任务 ID 不能为空")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".download")
        temporary.unlink(missing_ok=True)
        url = self.base_url + f"/v1/videos/{safe_id}/content"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=False,
                trust_env=not _is_loopback_url(url),
            ) as client:
                async with client.stream(
                    "GET", url, params={"index": max(1, int(index))}, headers={"Accept": "video/mp4", **self._headers()}
                ) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raw = await response.aread()
                        try:
                            payload: Any = json.loads(raw.decode("utf-8", errors="replace"))
                        except (ValueError, json.JSONDecodeError):
                            payload = raw.decode("utf-8", errors="replace")[:4_000]
                        raise FlowVideoError(
                            f"Flow 视频结果下载返回 HTTP {response.status_code}：{_error_text(payload, response.reason_phrase)}",
                            status_code=response.status_code,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > max_bytes:
                                raise FlowVideoError("Flow 视频结果超过 2 GiB，拒绝写入")
                        except ValueError:
                            pass
                    size = 0
                    with temporary.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if not chunk:
                                continue
                            size += len(chunk)
                            if size > max_bytes:
                                raise FlowVideoError("Flow 视频结果超过 2 GiB，拒绝写入")
                            handle.write(chunk)
        except FlowVideoError:
            temporary.unlink(missing_ok=True)
            raise
        except httpx.TimeoutException as exc:
            temporary.unlink(missing_ok=True)
            raise FlowVideoError(f"Flow 视频结果下载超时：{url}") from exc
        except httpx.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise FlowVideoError(f"Flow 视频结果下载失败：{url}；{exc}") from exc
        if size <= 0:
            temporary.unlink(missing_ok=True)
            raise FlowVideoError("Flow 视频结果为空")
        temporary.replace(target)
        return size
