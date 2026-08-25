"""Render Markdown into MarkFlow-like long PNG images locally.

The QQ client does not render Markdown consistently.  This module uses the
same browser-side libraries as the locally installed MarkFlow preview
(``marked``, ``highlight.js`` and KaTeX) and captures the resulting document
with Microsoft Edge's DevTools protocol.  Keeping rendering in a disposable
headless profile means no model-supplied Markdown is ever opened in the
operator's normal browser profile.

The renderer is deliberately a local capability rather than a web service:
the generated PNGs are returned to :class:`app.service.AgentService`, which
stores and sends them through the existing QQ image path.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union
from urllib.request import urlopen


DEFAULT_MARKFLOW_ROOT = Path("D:/项目/MarkFlow")
DEFAULT_RENDER_WIDTH = 1100
DEFAULT_RENDER_SLICE_HEIGHT = 10_000
MAX_MARKDOWN_CHARS = 160_000
MAX_RENDER_SLICES = 12
RENDER_READY_TIMEOUT_SECONDS = 35.0
# A disposable Edge profile is cheap to start, but it is still important not
# to turn one transient Chromium navigation race into an unbounded local retry
# loop.  One fresh-profile retry covers the common ``Execution context was
# destroyed`` failure without hiding persistent renderer problems.
MAX_TRANSIENT_CDP_RETRIES = 1


class MarkdownRenderError(RuntimeError):
    """The local MarkFlow-compatible renderer could not make an image.

    ``diagnostic`` deliberately contains stable, machine-readable fields.  It
    is returned by the service to the model/dashboard without relying on an
    Edge error sentence which can vary between browser versions.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "markdown_render_failed",
        stage: str = "render",
        transient: bool = False,
        attempts: int = 1,
        retry_attempted: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "markdown_render_failed")
        self.stage = str(stage or "render")
        self.transient = bool(transient)
        self.attempts = max(1, int(attempts))
        self.retry_attempted = bool(retry_attempted)

    def diagnostic(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "transient": self.transient,
            "attempts": self.attempts,
            "local_retry_attempted": self.retry_attempted,
        }


def _is_transient_cdp_context_error(value: Union[BaseException, str]) -> bool:
    """Return whether a CDP navigation/context race merits one fresh retry."""

    text = str(value).casefold()
    # These messages are produced by Chromium CDP when a first-page
    # navigation races a Runtime/Page command.  Do not include broad errors
    # such as arbitrary websocket timeouts: they need an actionable diagnostic
    # rather than a blind duplicate browser launch.
    return any(
        marker in text
        for marker in (
            "execution context was destroyed",
            "cannot find context with specified id",
            "inspected target navigated or closed",
            "not attached to an active page",
        )
    )


def _error_with_attempts(error: MarkdownRenderError, attempts: int, retried: bool) -> MarkdownRenderError:
    """Preserve a stable root diagnostic while recording the bounded retry."""

    transient = bool(error.transient or _is_transient_cdp_context_error(error))
    return MarkdownRenderError(
        str(error),
        code=error.code,
        stage=error.stage,
        transient=transient,
        attempts=attempts,
        retry_attempted=retried,
    )


@dataclass(frozen=True)
class RenderedMarkdownImage:
    """One temporary rendered image, ready to be put in ``MediaStore``."""

    path: Path
    width: int
    height: int
    index: int
    total: int


@dataclass(frozen=True)
class MarkdownRenderResult:
    """Images plus the temporary job directory that owns them."""

    job_dir: Path
    images: Sequence[RenderedMarkdownImage]


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _detect_markflow_root(explicit_root: Optional[Path | str] = None) -> Path:
    candidates: List[Path] = []
    if explicit_root:
        candidates.append(Path(explicit_root))
    configured = os.environ.get("QQ_AI_MARKFLOW_ROOT", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.append(DEFAULT_MARKFLOW_ROOT)
    for candidate in candidates:
        root = candidate.expanduser()
        required = (
            root / "vendor" / "markdown" / "marked.umd.js",
            root / "vendor" / "markdown" / "purify.min.js",
            root / "vendor" / "markdown" / "highlight.min.js",
            root / "vendor" / "markdown" / "katex" / "katex.min.js",
            root / "vendor" / "markdown" / "katex" / "katex.min.css",
        )
        if all(item.is_file() for item in required):
            return root.resolve()
    searched = ", ".join(str(item) for item in candidates)
    raise MarkdownRenderError(
        "未找到 MarkFlow 渲染资源。请保留 MarkFlow 的 vendor/markdown 目录，"
        "或设置 QQ_AI_MARKFLOW_ROOT；已检查：" + searched
    )


def _find_browser(explicit_browser: Optional[Path | str] = None) -> Path:
    candidates: List[Path] = []
    if explicit_browser:
        candidates.append(Path(explicit_browser))
    configured = os.environ.get("QQ_AI_MARKDOWN_RENDER_BROWSER", "").strip()
    if configured:
        candidates.append(Path(configured))
    for name in ("msedge", "msedge.exe", "chrome", "chrome.exe", "chromium", "chromium.exe"):
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
    candidates.extend(
        (
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        )
    )
    for candidate in candidates:
        try:
            if candidate.expanduser().is_file():
                return candidate.expanduser().resolve()
        except OSError:
            continue
    raise MarkdownRenderError(
        "未找到可用于本地 Markdown 渲染的 Microsoft Edge/Chrome。"
        "请安装 Edge，或设置 QQ_AI_MARKDOWN_RENDER_BROWSER。"
    )


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _json_for_script(value: Any) -> str:
    """Serialize data safely inside an inline ``<script>`` element."""

    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


# This is intentionally the preview portion of MarkFlow's visual language,
# not a generic GitHub Markdown stylesheet.  It mirrors its light page, 920px
# article, lavender quote cards, dark traffic-light code windows, and KaTeX
# layout closely enough that the QQ image looks like the right-side preview.
MARKFLOW_PREVIEW_CSS = r"""
:root { --page:#f4f5f7; --surface:#fff; --ink:#1d2433; --muted:#768093;
  --line:#e6e8ed; --accent:#596bff; --code-bg:#11141b; --code-top:#1a1e27; }
* { box-sizing:border-box; }
html, body { width:100%; min-height:100%; margin:0; }
body { color:var(--ink); background:var(--page); font-family:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; -webkit-font-smoothing:antialiased; }
.canvas { min-height:100vh; padding:30px 18px; }
.markdown-body { width:min(100%,920px); min-height:0; margin:0 auto; padding:30px clamp(28px,5vw,68px) 90px; color:#2b3342; background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:0 18px 48px rgba(31,38,51,.08); font-size:16px; line-height:1.78; overflow-wrap:break-word; }
.markdown-body > :first-child { margin-top:0!important; }
.markdown-body > :last-child { margin-bottom:0!important; }
.markdown-body h1,.markdown-body h2,.markdown-body h3,.markdown-body h4,.markdown-body h5,.markdown-body h6 { color:#171d29; font-weight:760; line-height:1.35; letter-spacing:-.025em; }
.markdown-body h1 { margin:0 0 26px; padding-bottom:16px; border-bottom:1px solid var(--line); font-size:42px; }
.markdown-body h2 { margin:42px 0 16px; padding-bottom:9px; border-bottom:1px solid #edf0f3; font-size:26px; }
.markdown-body h3 { margin:32px 0 12px; font-size:21px; }
.markdown-body h4 { margin:26px 0 10px; font-size:17px; }
.markdown-body h5 { margin:22px 0 8px; font-size:15px; }
.markdown-body h6 { margin:20px 0 8px; color:#5b6576; font-size:14px; }
.markdown-body p,.markdown-body ul,.markdown-body ol,.markdown-body blockquote,.markdown-body table,.markdown-body .code-window,.markdown-body .math-display { margin-top:0; margin-bottom:20px; }
.markdown-body a { color:#4c5fe7; text-decoration:none; border-bottom:1px solid rgba(76,95,231,.25); }
.markdown-body strong { color:#1c2330; font-weight:750; }
.markdown-body mark { padding:.08em .28em; border-radius:5px; background:#fff2a8; }
.markdown-body del { color:#8b93a1; }
.markdown-body hr { height:1px; margin:36px 0; border:0; background:linear-gradient(90deg,transparent,#d8dce4 12%,#d8dce4 88%,transparent); }
.markdown-body blockquote { padding:14px 18px; border-left:4px solid #7b88ff; border-radius:0 10px 10px 0; color:#5b6576; background:#f6f7ff; }
.markdown-body blockquote > :last-child { margin-bottom:0; }
.markdown-body ul,.markdown-body ol { padding-left:1.6em; }
.markdown-body li { margin:5px 0; padding-left:.2em; }
.markdown-body li::marker { color:#6977e9; font-weight:700; }
.markdown-body li > ul,.markdown-body li > ol { margin-top:5px; margin-bottom:5px; }
.markdown-body .task-list-item { list-style:none; }
.markdown-body .task-list-item input { width:15px; height:15px; margin:0 8px 0 -1.4em; vertical-align:-2px; accent-color:var(--accent); }
.markdown-body :not(pre)>code { padding:.18em .42em; border:1px solid #e2e5eb; border-radius:6px; color:#d64067; background:#f5f6f8; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:.88em; }
.code-window { overflow:hidden; border:1px solid #262b36; border-radius:13px; background:var(--code-bg); box-shadow:0 16px 34px rgba(17,20,27,.15); }
.code-window-toolbar { display:flex; align-items:center; height:42px; padding:0 14px; border-bottom:1px solid rgba(255,255,255,.06); background:var(--code-top); }
.traffic-lights { display:flex; align-items:center; gap:7px; }
.traffic-light { width:12px; height:12px; border-radius:50%; box-shadow:inset 0 -1px 1px rgba(0,0,0,.16); }
.traffic-light.red { background:#ff5f57; }.traffic-light.yellow { background:#febc2e; }.traffic-light.green { background:#28c840; }
.code-language { margin-left:14px; color:#7f899d; font-family:"SFMono-Regular",Consolas,monospace; font-size:10px; letter-spacing:.08em; text-transform:uppercase; }
.code-window pre { margin:0; overflow:visible; background:var(--code-bg); }
.code-window pre code { display:block; padding:20px 22px 22px; color:#d8dee9; background:transparent; font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; font-size:13.5px; line-height:1.72; tab-size:4; white-space:pre-wrap; overflow-wrap:anywhere; }
.hljs-comment,.hljs-quote { color:#697386; font-style:italic; }.hljs-keyword,.hljs-selector-tag,.hljs-literal,.hljs-section,.hljs-link { color:#c792ea; }.hljs-string,.hljs-title,.hljs-name,.hljs-type,.hljs-attribute,.hljs-symbol,.hljs-bullet,.hljs-addition,.hljs-variable,.hljs-template-tag,.hljs-template-variable { color:#addb67; }.hljs-number,.hljs-meta,.hljs-built_in,.hljs-builtin-name,.hljs-params { color:#f78c6c; }.hljs-title.function_,.hljs-function .hljs-title,.hljs-selector-id,.hljs-selector-class { color:#82aaff; }.hljs-attr,.hljs-property,.hljs-regexp { color:#ffcb6b; }.hljs-deletion { color:#ff5370; }
.markdown-body table { display:table; width:max-content; max-width:none; border-spacing:0; border-collapse:separate; border:1px solid var(--line); border-radius:10px; font-size:14px; }
.markdown-table-scroll { display:block; max-width:100%; margin:0 0 20px; overflow:hidden; border:1px solid var(--line); border-radius:10px; background:rgba(255,255,255,.46); }.markdown-table-scroll table { margin:0; border:0; border-radius:0; }
.markdown-body th,.markdown-body td { padding:10px 14px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); text-align:left; white-space:normal; }.markdown-body th { color:#424b5c; background:#f7f8fa; font-weight:700; }.markdown-body tr:last-child td { border-bottom:0; }.markdown-body th:last-child,.markdown-body td:last-child { border-right:0; }
.markdown-body img { display:block; max-width:100%; height:auto; margin:24px auto; border-radius:10px; box-shadow:0 8px 26px rgba(31,38,51,.11); }
.markdown-body .katex-display { margin:0; padding:8px 0; overflow:hidden; }.markdown-body .math-error { color:#d94a63; }
@media(max-width:640px) { .canvas { padding:0; }.markdown-body { padding:26px 22px 70px; border:0; border-radius:0; box-shadow:none; }.markdown-body h1 { font-size:32px; } }
"""


def _standalone_html(markdown: str, markflow_root: Path) -> str:
    vendor = markflow_root / "vendor" / "markdown"
    assets = {
        "marked": _file_uri(vendor / "marked.umd.js"),
        "purify": _file_uri(vendor / "purify.min.js"),
        "highlight": _file_uri(vendor / "highlight.min.js"),
        "katex_js": _file_uri(vendor / "katex" / "katex.min.js"),
        "katex_css": _file_uri(vendor / "katex" / "katex.min.css"),
    }
    source = _json_for_script(markdown)
    return """<!doctype html>
<html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<link rel=\"stylesheet\" href=\"{katex_css}\"><style>{css}</style></head>
<body><main class=\"canvas\"><article class=\"markdown-body\" id=\"preview\"></article></main>
<script src=\"{marked}\"></script><script src=\"{purify}\"></script><script src=\"{highlight}\"></script><script src=\"{katex_js}\"></script>
<script>
(() => {{
  const source = {source};
  const preview = document.getElementById('preview');
  window.__markdownRenderReady = false;
  window.__markdownRenderError = '';
  function esc(value) {{ const item = document.createElement('span'); item.textContent = value || ''; return item.innerHTML; }}
  function lang(code) {{ for (const name of code.classList) if (name.startsWith('language-')) return name.slice(9); return 'code'; }}
  function enhanceCodeBlocks(root) {{
    root.querySelectorAll('pre > code').forEach((code) => {{
      const pre = code.parentElement;
      try {{ if (code.textContent.length <= 60000) hljs.highlightElement(code); }} catch (_) {{}}
      const box = document.createElement('div'); box.className = 'code-window';
      const bar = document.createElement('div'); bar.className = 'code-window-toolbar';
      bar.innerHTML = '<div class=\"traffic-lights\"><span class=\"traffic-light red\"></span><span class=\"traffic-light yellow\"></span><span class=\"traffic-light green\"></span></div><span class=\"code-language\">' + esc(lang(code)) + '</span>';
      pre.replaceWith(box); box.append(bar, pre);
    }});
  }}
  function renderMath(root) {{
    root.querySelectorAll('[data-math]').forEach((node) => {{
      try {{ katex.render(decodeURIComponent(node.dataset.math), node, {{ displayMode:node.classList.contains('math-display'), throwOnError:false, errorColor:'#d94a63' }}); }}
      catch (_) {{ node.classList.add('math-error'); node.textContent = decodeURIComponent(node.dataset.math); }}
    }});
  }}
  try {{
    marked.setOptions({{ gfm:true, breaks:true }});
    marked.use({{ extensions:[
      {{ name:'blockMath', level:'block', start(s) {{ const a=s.indexOf('$$'), b=s.indexOf('\\\\['); return a<0?b:(b<0?a:Math.min(a,b)); }}, tokenizer(s) {{ const m=/^\\$\\$[ \\t]*(?:\\n)?([\\s\\S]*?)(?:\\n)?\\$\\$(?:\\n|$)/.exec(s) || /^\\\\\\[[ \\t]*(?:\\n)?([\\s\\S]*?)(?:\\n)?\\\\\\](?:\\n|$)/.exec(s); if (!m) return; return {{ type:'blockMath', raw:m[0], text:m[1].trim() }}; }}, renderer(t) {{ return '<div class=\"math-display\" data-math=\"' + encodeURIComponent(t.text) + '\"></div>'; }} }},
      {{ name:'inlineMath', level:'inline', start(s) {{ const a=s.indexOf('$'), b=s.indexOf('\\\\('); return a<0?b:(b<0?a:Math.min(a,b)); }}, tokenizer(s) {{ const m=/^\\$(?!\\$)((?:\\\\.|[^\\\\$\\n])+?)\\$(?!\\$)/.exec(s) || /^\\\\\\(([^\\n]*?)\\\\\\)/.exec(s); if (!m) return; return {{ type:'inlineMath', raw:m[0], text:m[1].trim() }}; }}, renderer(t) {{ return '<span class=\"math-inline\" data-math=\"' + encodeURIComponent(t.text) + '\"></span>'; }} }}
    ] }});
    const raw = marked.parse(source);
    preview.innerHTML = DOMPurify.sanitize(raw, {{ ADD_ATTR:['target','data-math'], ADD_TAGS:['mark'] }});
    // The renderer is allowed to load its own local MarkFlow JavaScript and
    // fonts, but user/LLM Markdown must never turn a file:// image into a QQ
    // screenshot of an arbitrary local path.  Keep normal public Markdown
    // images and embedded image data, reject every other image scheme.
    preview.querySelectorAll('img').forEach((image) => {{ const src=(image.getAttribute('src') || '').trim(); if (!/^(https?:|data:image\\/)/i.test(src)) image.remove(); }});
    preview.querySelectorAll('table').forEach((table) => {{ const wrap=document.createElement('div'); wrap.className='markdown-table-scroll'; table.replaceWith(wrap); wrap.append(table); }});
    renderMath(preview); enhanceCodeBlocks(preview);
    window.__markdownRenderReady = true;
  }} catch (error) {{ window.__markdownRenderError = String(error && (error.stack || error.message) || error); window.__markdownRenderReady = true; }}
}})();
</script></body></html>""".format(css=MARKFLOW_PREVIEW_CSS, source=source, **assets)


async def _cdp_capture(port: int, width: int, output_dir: Path) -> List[Tuple[Path, int, int]]:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - project environment bundles this dependency
        raise MarkdownRenderError("缺少 websockets，无法连接本地 Edge 渲染器") from exc

    deadline = time.monotonic() + RENDER_READY_TIMEOUT_SECONDS
    target: Dict[str, Any] = {}
    endpoint = "http://127.0.0.1:%s/json/list" % port
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint, timeout=1.5) as response:  # noqa: S310 - fixed loopback endpoint
                candidates = json.loads(response.read().decode("utf-8"))
            if isinstance(candidates, list):
                target = next(
                    (item for item in candidates if isinstance(item, dict) and item.get("type") == "page" and item.get("webSocketDebuggerUrl")),
                    {},
                )
                if target:
                    break
        except Exception as exc:  # browser still starting
            last_error = str(exc)
        await asyncio.sleep(0.1)
    if not target:
        raise MarkdownRenderError("Edge 无法启动本地渲染页" + ("：" + last_error if last_error else ""))

    websocket_url = str(target["webSocketDebuggerUrl"])
    request_id = 0
    async with websockets.connect(websocket_url, max_size=None, ping_interval=None) as socket_client:
        async def call(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            nonlocal request_id
            request_id += 1
            current = request_id
            await socket_client.send(json.dumps({"id": current, "method": method, "params": params or {}}))
            while True:
                response = json.loads(await socket_client.recv())
                if response.get("id") != current:
                    continue
                if response.get("error"):
                    detail = str(response["error"].get("message") or response["error"])
                    transient = _is_transient_cdp_context_error(detail)
                    raise MarkdownRenderError(
                        "Edge 渲染协议错误：" + detail,
                        code=("edge_cdp_context_destroyed" if transient else "edge_cdp_protocol_error"),
                        stage="cdp",
                        transient=transient,
                    )
                result = response.get("result")
                return result if isinstance(result, dict) else {}

        await call("Page.enable")
        await call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False, "screenWidth": width, "screenHeight": 900},
        )
        ready = await call(
            "Runtime.evaluate",
            {
                "expression": "new Promise(resolve => { const tick = () => { if (window.__markdownRenderReady) { Promise.resolve(document.fonts && document.fonts.ready).then(resolve); } else { setTimeout(tick, 20); } }; tick(); })",
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        error_value = await call(
            "Runtime.evaluate",
            {"expression": "window.__markdownRenderError || ''", "returnByValue": True},
        )
        runtime_error = (
            error_value.get("result", {}).get("value", "")
            if isinstance(error_value.get("result"), dict)
            else ""
        )
        if runtime_error:
            raise MarkdownRenderError("MarkFlow 预览脚本渲染失败：" + str(runtime_error)[:1_000])
        # Wait for the two final frames needed for code highlighting and KaTeX
        # metrics to settle after the document fonts are ready.
        await call(
            "Runtime.evaluate",
            {"expression": "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))", "awaitPromise": True},
        )
        metrics = await call("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
        # ``.canvas`` intentionally has ``min-height: 100vh`` for the live
        # MarkFlow app.  A short QQ response should not therefore turn into a
        # mostly blank 900px card; crop to the rendered article plus its
        # natural bottom margin while retaining the exact long-document size.
        rendered_bounds = await call(
            "Runtime.evaluate",
            {
                "expression": "(() => { const article=document.getElementById('preview'); const canvas=document.querySelector('.canvas'); const a=article ? article.getBoundingClientRect() : {bottom:0}; const c=canvas ? canvas.getBoundingClientRect() : {width:0}; return {width:Math.ceil(Math.max(document.documentElement.scrollWidth || 0, c.width || 0)), height:Math.ceil(Math.max(1, a.bottom + 30))}; })()",
                "returnByValue": True,
            },
        )
        bounds_value = rendered_bounds.get("result", {}).get("value", {})
        if not isinstance(bounds_value, dict):
            bounds_value = {}
        page_width = max(width, int(float(bounds_value.get("width") or size.get("width", width))))
        page_height = max(1, int(float(bounds_value.get("height") or size.get("height", 1))))
        slice_count = (page_height + DEFAULT_RENDER_SLICE_HEIGHT - 1) // DEFAULT_RENDER_SLICE_HEIGHT
        if slice_count > MAX_RENDER_SLICES:
            raise MarkdownRenderError(
                "Markdown 渲染结果过长（约 %s px），最多允许 %s 张长图；请拆分内容后重试。"
                % (page_height, MAX_RENDER_SLICES)
            )
        captured: List[Tuple[Path, int, int]] = []
        for index in range(slice_count):
            y = index * DEFAULT_RENDER_SLICE_HEIGHT
            height = min(DEFAULT_RENDER_SLICE_HEIGHT, page_height - y)
            # Chromium/Edge occasionally returns an empty compositor surface
            # when ``captureBeyondViewport`` is asked for a clip shorter than
            # its 900px emulated viewport.  Capture a full viewport for short
            # documents instead; the extra light canvas padding is preferable
            # to silently sending a blank QQ image.
            capture_height = max(900, height)
            payload = await call(
                "Page.captureScreenshot",
                {
                    "format": "png",
                    "fromSurface": True,
                    "captureBeyondViewport": True,
                    "clip": {"x": 0, "y": y, "width": page_width, "height": capture_height, "scale": 1},
                },
            )
            encoded = payload.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise MarkdownRenderError("Edge 没有返回 Markdown 图片数据")
            try:
                binary = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise MarkdownRenderError("Edge 返回的 Markdown 图片数据无效") from exc
            target_path = output_dir / ("markdown-%02d.png" % (index + 1))
            target_path.write_bytes(binary)
            if len(binary) < 16 or not binary.startswith(b"\x89PNG\r\n\x1a\n"):
                raise MarkdownRenderError("Edge 返回的不是 PNG 图片")
            captured.append((target_path, page_width, capture_height))
    return captured


class MarkFlowMarkdownRenderer:
    """Create MarkFlow-style PNGs using local browser assets."""

    def __init__(
        self,
        output_root: Path | str,
        *,
        markflow_root: Optional[Path | str] = None,
        browser_path: Optional[Path | str] = None,
        width: int = DEFAULT_RENDER_WIDTH,
    ) -> None:
        self.output_root = Path(output_root)
        self.markflow_root = Path(markflow_root) if markflow_root else None
        self.browser_path = Path(browser_path) if browser_path else None
        self.width = max(720, min(int(width), 2_000))

    def _render_once(
        self,
        markdown: str,
        markflow_root: Path,
        browser: Path,
        job_dir: Path,
    ) -> MarkdownRenderResult:
        """Render once in a disposable Edge profile; never retry in here."""

        profile_dir = job_dir / "edge-profile"
        html_path = job_dir / "preview.html"
        html_path.write_text(_standalone_html(markdown, markflow_root), encoding="utf-8")
        port = _free_loopback_port()
        process: Optional[subprocess.Popen[bytes]] = None
        try:
            process = subprocess.Popen(
                [
                    str(browser),
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--allow-file-access-from-files",
                    "--disable-extensions",
                    "--remote-allow-origins=http://127.0.0.1",
                    "--remote-debugging-address=127.0.0.1",
                    "--remote-debugging-port=%s" % port,
                    "--user-data-dir=%s" % profile_dir,
                    _file_uri(html_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            captured = asyncio.run(_cdp_capture(port, self.width, job_dir))
            total = len(captured)
            return MarkdownRenderResult(
                job_dir=job_dir,
                images=tuple(
                    RenderedMarkdownImage(path=path, width=width, height=height, index=index, total=total)
                    for index, (path, width, height) in enumerate(captured, 1)
                ),
            )
        finally:
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Edge keeps renderer child processes alive briefly on
                    # Windows.  Kill the disposable job's process tree so a
                    # burst of Markdown replies cannot accumulate profiles or
                    # leave the next local render waiting for stale children.
                    if os.name == "nt":
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=3,
                                check=False,
                            )
                        except Exception:
                            pass
                    try:
                        process.kill()
                    except Exception:
                        pass
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

    def render(self, markdown: str) -> MarkdownRenderResult:
        if not isinstance(markdown, str):
            raise MarkdownRenderError("markdown 必须是字符串", code="invalid_markdown", stage="validation")
        if not markdown.strip():
            raise MarkdownRenderError("markdown 不能为空", code="invalid_markdown", stage="validation")
        if len(markdown) > MAX_MARKDOWN_CHARS:
            raise MarkdownRenderError(
                "markdown 超过 %s 字符，请拆分后渲染" % MAX_MARKDOWN_CHARS,
                code="markdown_too_large",
                stage="validation",
            )
        markflow_root = _detect_markflow_root(self.markflow_root)
        browser = _find_browser(self.browser_path)
        self.output_root.mkdir(parents=True, exist_ok=True)

        for attempt in range(1, MAX_TRANSIENT_CDP_RETRIES + 2):
            job_dir = Path(tempfile.mkdtemp(prefix="markdown-render-", dir=str(self.output_root)))
            try:
                result = self._render_once(markdown, markflow_root, browser, job_dir)
                return result
            except MarkdownRenderError as exc:
                shutil.rmtree(job_dir, ignore_errors=True)
                transient = bool(exc.transient or _is_transient_cdp_context_error(exc))
                if transient and attempt <= MAX_TRANSIENT_CDP_RETRIES:
                    continue
                raise _error_with_attempts(exc, attempt, attempt > 1) from exc
            except Exception as exc:
                shutil.rmtree(job_dir, ignore_errors=True)
                error = MarkdownRenderError(
                    "Markdown 图片渲染失败：%s" % exc,
                    code=("edge_cdp_context_destroyed" if _is_transient_cdp_context_error(exc) else "markdown_render_unexpected_error"),
                    stage="cdp" if _is_transient_cdp_context_error(exc) else "render",
                    transient=_is_transient_cdp_context_error(exc),
                )
                if error.transient and attempt <= MAX_TRANSIENT_CDP_RETRIES:
                    continue
                raise _error_with_attempts(error, attempt, attempt > 1) from exc


def render_markdown_images(
    markdown: str,
    output_root: Path | str,
    *,
    markflow_root: Optional[Path | str] = None,
    browser_path: Optional[Path | str] = None,
    width: int = DEFAULT_RENDER_WIDTH,
) -> MarkdownRenderResult:
    """Convenience function used by the async service through ``to_thread``."""

    return MarkFlowMarkdownRenderer(
        output_root,
        markflow_root=markflow_root,
        browser_path=browser_path,
        width=width,
    ).render(markdown)


__all__ = [
    "DEFAULT_MARKFLOW_ROOT",
    "DEFAULT_RENDER_SLICE_HEIGHT",
    "DEFAULT_RENDER_WIDTH",
    "MAX_MARKDOWN_CHARS",
    "MarkdownRenderError",
    "MarkdownRenderResult",
    "MarkFlowMarkdownRenderer",
    "RenderedMarkdownImage",
    "render_markdown_images",
]
