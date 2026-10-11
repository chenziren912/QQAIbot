"""Compact model-facing tools backed by the existing validated executors."""

from copy import deepcopy
import json


ROUTES = {
    "Builtin_send": ("type", {
        "text": "send_group_message",
        "markdown": "Builtin_render_markdown_image",
        "file": "send_group_file",
    }),
    "Builtin_video_download": ("action", {
        "youtube": "Builtin_youtube_download",
        "bilibili": "Builtin_bilibili_download",
        "confirm": "Builtin_video_download_confirmation",
    }),
    "Builtin_video_generation": ("action", {
        "create": "Builtin_video_generation_create",
        "list": "Builtin_video_generation_list",
        "result": "Builtin_video_generation_result",
    }),
}
DESCRIPTIONS = {
    "Builtin_send": "向当前群聊或私聊发送内容。type=text 使用 text（单条最多4000字符，可传 reply_to_message_id）；type=markdown 使用 markdown 渲染图片；type=file 使用当前会话工作区相对 path（可传 name），视频自动 QQ 兼容处理，音频转语音。引用只能使用可信当前会话消息 ID。不得发送内部摘要。",
    "Builtin_video_download": "下载视频到当前会话工作区，不自动发送。action=download 时用 channel 选择 youtube/bilibili，传 url 或 query，默认最高720P、优先H.264。没有H.264时只返回确认记录和询问文本；必须先询问成员，收到明确同意后用 action=confirm、confirmation_id、decision=approve 继续，拒绝用 decline，不明确用 unclear。不得编造确认ID或绕过确认。",
    "Builtin_video_generation": "Google Flow AI 浏览器视频子任务。action=create 使用 prompt 启动独立 AI 操作 Flow，模型仅允许 Gemini Omni Flash 1.1；页面证实任务已提交后才返回任务 ID，然后结束当前轮。审查子任务每 5 秒观察同一标签页，完成后点击下载到工作区并按设置发送视频。action=list 查询当前会话任务；action=result 使用 job_id 查真实观察、进度、错误和发送结果，明确要求补发才传 send=true。不得根据旧回复猜测状态。",
}
REVERSE = {old: (public, selector, value) for public, (selector, routes) in ROUTES.items() for value, old in routes.items()}


def model_tools(tools):
    """Merge only branches present in the input (including recovery subsets)."""
    flat = bool(tools and "function" not in tools[0])
    functions = [t.get("function", t) for t in tools]
    present = {f["name"]: f for f in functions}
    result, emitted = [], set()
    for f in functions:
        name = f["name"]
        if name not in REVERSE:
            merged = deepcopy(f)
        else:
            public, selector, _ = REVERSE[name]
            if public in emitted:
                continue
            emitted.add(public)
            branches = {v: old for v, old in ROUTES[public][1].items() if old in present}
            properties = {}
            for old in branches.values():
                properties.update(deepcopy(present[old]["parameters"].get("properties", {})))
            if public == "Builtin_video_download":
                channels = [v for v in branches if v != "confirm"]
                actions = (["download"] if channels else []) + (["confirm"] if "confirm" in branches else [])
                properties[selector] = {"type": "string", "enum": actions}
                if channels:
                    properties["channel"] = {"type": "string", "enum": channels, "description": "action=download 时必填。"}
            else:
                properties[selector] = {"type": "string", "enum": list(branches)}
            merged = {"name": public, "description": DESCRIPTIONS[public], "parameters": {
                "type": "object", "properties": properties, "required": [selector], "additionalProperties": False,
            }}
        merged["description"] = rewrite_references(merged.get("description", ""))
        result.append({"type": "function", **merged} if flat else {"type": "function", "function": merged})
    return result


def normalise_call(name, arguments, legacy_tools):
    if name not in ROUTES:
        return name, arguments
    if not isinstance(arguments, dict):
        raise ValueError("工具参数必须是 JSON 对象")
    selector, routes = ROUTES[name]
    value = arguments.get(selector)
    args = dict(arguments)
    args.pop(selector, None)
    if name == "Builtin_video_download":
        if value == "download":
            value = args.pop("channel", None)
            if not isinstance(value, str) or value not in {"youtube", "bilibili"}:
                raise ValueError("下载必须选择 channel=youtube 或 bilibili")
        elif value != "confirm":
            raise ValueError("action 必须是 download 或 confirm")
    if not isinstance(value, str) or value not in routes:
        raise ValueError("不支持的工具操作")
    old = routes[value]
    schema = next(t["function"]["parameters"] for t in legacy_tools if t["function"]["name"] == old)
    unknown = set(args) - set(schema.get("properties", {}))
    if unknown:
        raise ValueError("该操作不接受参数：" + ", ".join(sorted(unknown)))
    if any(k not in args for k in schema.get("required", [])):
        raise ValueError("缺少该操作的必填参数：" + ", ".join(schema.get("required", [])))
    return old, args


def public_call(name, arguments):
    if name not in REVERSE:
        return name, arguments
    public, selector, value = REVERSE[name]
    args = dict(arguments)
    if public == "Builtin_video_download" and value != "confirm":
        args.update(action="download", channel=value)
    else:
        args[selector] = value
    return public, args


def rewrite_references(text):
    for old in sorted(REVERSE, key=len, reverse=True):
        public, args = public_call(old, {})
        suffix = ",".join(f'{k}="{v}"' for k, v in args.items())
        text = text.replace(old, f"{public}({suffix})")
    return text


def prepare_model_request(body):
    body = deepcopy(body)
    if "tools" in body:
        body["tools"] = model_tools(body["tools"])
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        function = choice.get("function", choice)
        if function.get("name") in REVERSE:
            function["name"] = REVERSE[function["name"]][0]
    if isinstance(body.get("instructions"), str):
        body["instructions"] = rewrite_references(body["instructions"])
    for item in body.get("messages", []) + (body.get("input", []) if isinstance(body.get("input"), list) else []):
        if item.get("role") in {"developer", "system"} and isinstance(item.get("content"), str):
            item["content"] = rewrite_references(item["content"])
        calls = item.get("tool_calls", [])
        if item.get("type") == "function_call":
            calls = [{"function": item}]
        for call in calls:
            f = call.get("function", {})
            if f.get("name") in REVERSE:
                args = f.get("arguments", "{}")
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                    if not isinstance(args, dict):
                        raise ValueError("historical arguments are not an object")
                    f["name"], args = public_call(f["name"], args)
                    f["arguments"] = json.dumps(args, ensure_ascii=False)
                except (TypeError, ValueError):
                    # Preserve invalid history so the following tool error can
                    # still reach the model; never execute or silently repair it.
                    f["name"] = REVERSE[f["name"]][0]
    return body
