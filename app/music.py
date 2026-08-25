"""Download music and prepare it for a QQ voice message."""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Union
from urllib.parse import urlsplit


class MusicDownloadError(RuntimeError):
    """A bounded, model-readable download or conversion failure."""


@dataclass(frozen=True)
class DownloadedMusic:
    path: Path
    title: str
    source_url: str


@dataclass(frozen=True)
class VoiceSegment:
    """One QQ-record-compatible audio segment."""

    path: Path
    index: int
    start_seconds: float


DEFAULT_VOICE_SEGMENT_SECONDS = 50


_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._()\- ]+")


def _clean_title(value: str) -> str:
    text = _SAFE_FILENAME.sub("_", str(value or "音乐")).strip(" .")
    return (text or "音乐")[:100]


def _validate_url(value: str) -> str:
    url = str(value or "").strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise MusicDownloadError("音乐链接格式无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise MusicDownloadError("音乐链接必须是 http(s) URL")
    return url


def _validate_query(value: str) -> str:
    query = " ".join(str(value or "").split()).strip()
    if not query:
        raise MusicDownloadError("音乐搜索词不能为空")
    if len(query) > 500:
        raise MusicDownloadError("音乐搜索词最多 500 个字符")
    return query


def _run(command: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MusicDownloadError("找不到执行文件：%s" % command[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise MusicDownloadError("音乐下载或转码超时（%.0f 秒）" % timeout) from exc
    if result.returncode != 0:
        output = (result.stdout or "").strip()
        if len(output) > 4_000:
            output = output[-4_000:]
        raise MusicDownloadError(
            "%s 失败（退出码 %s）：%s"
            % (Path(str(command[0])).name, result.returncode, output or "未返回错误详情")
        )
    return result


def download_music(
    url: str,
    output_dir: Union[Path, str],
    *,
    yt_dlp_command: Sequence[str],
    ffmpeg_path: str,
    cookies_file: Optional[Union[Path, str]] = None,
    proxy: str = "",
    preferred_title: str = "",
    query: str = "",
    timeout_seconds: float = 600,
) -> DownloadedMusic:
    """Download one public track and transcode it to a mono MP3.

    NapCat accepts a local MP3 as a OneBot record message segment and performs
    the final QQ voice/silk conversion.
    """

    if str(url or "").strip() and str(query or "").strip():
        raise MusicDownloadError("音乐下载只能提供 URL 或搜索词之一")
    if str(url or "").strip():
        target_url = _validate_url(url)
        search_mode = False
        try:
            target_host = (urlsplit(target_url).hostname or "").lower()
        except ValueError:
            target_host = ""
        bilibili_mode = target_host == "bilibili.com" or target_host.endswith(".bilibili.com") or target_host in {"b23.tv", "bili2233.cn"}
    else:
        search_query = _validate_query(query)
        # Bilibili's yt-dlp search extractor is more reliable than the
        # YouTube search extractor in the user's current environment.  The
        # service still returns the actual yt-dlp diagnostic if Bilibili is
        # unavailable or the query has no result.
        target_url = "bilisearch1:" + search_query
        search_mode = True
        bilibili_mode = True
    command_prefix = [str(item) for item in yt_dlp_command if str(item)]
    if not command_prefix:
        raise MusicDownloadError("未配置 yt-dlp")
    if not str(ffmpeg_path or "").strip():
        raise MusicDownloadError("未找到 ffmpeg，无法转码为 QQ 语音")
    ffmpeg = str(ffmpeg_path)
    if not Path(ffmpeg).exists() and not shutil.which(ffmpeg):
        raise MusicDownloadError("未找到 ffmpeg：%s" % ffmpeg)

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    job_dir = Path(tempfile.mkdtemp(prefix="music-download-", dir=str(root)))
    try:
        template = job_dir / "source.%(ext)s"
        command: List[str] = command_prefix + [
            "--no-playlist",
            "--newline",
            "--no-warnings",
            "--socket-timeout",
            "30",
            "--print",
            "after_move:title",
            "--print",
            "after_move:filepath",
            "-f",
            "bestaudio/best",
            "-o",
            str(template),
        ]
        if search_mode:
            command.append("--playlist-end")
            command.append("1")
        if bilibili_mode:
            command.extend(
                [
                    "--user-agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "--referer",
                    "https://www.bilibili.com/",
                ]
            )
        cookie_path = Path(cookies_file) if cookies_file else None
        if cookie_path and cookie_path.is_file():
            command.extend(["--cookies", str(cookie_path)])
        if proxy:
            command.extend(["--proxy", str(proxy)])
        command.append(target_url)
        result = _run(command, timeout=timeout_seconds)

        source_candidates = [
            item for item in job_dir.iterdir()
            if item.is_file() and item.name.startswith("source.") and not item.name.endswith(".part")
        ]
        output_lines = [line.strip().strip('"') for line in (result.stdout or "").splitlines() if line.strip()]
        if not source_candidates:
            for line in output_lines:
                candidate = Path(line)
                if candidate.is_file():
                    source_candidates.append(candidate)
        if not source_candidates:
            raise MusicDownloadError("yt-dlp 下载完成但没有找到音频输出文件")
        source = max(source_candidates, key=lambda item: item.stat().st_mtime)

        reported_title = ""
        for line in output_lines:
            candidate = Path(line)
            if candidate.exists() or line.startswith("[") or line.lower().startswith("warning"):
                continue
            if len(line) <= 200:
                reported_title = line
        title = _clean_title(preferred_title or reported_title or "音乐")
        destination = root / (title + ".mp3")
        suffix = 2
        while destination.exists():
            destination = root / ("%s (%s).mp3" % (title, suffix))
            suffix += 1

        _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(source),
                "-vn",
                "-map_metadata",
                "-1",
                "-ac",
                "1",
                "-ar",
                "24000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "32k",
                str(destination),
            ],
            timeout=timeout_seconds,
        )
        if not destination.is_file() or destination.stat().st_size <= 0:
            raise MusicDownloadError("ffmpeg 转码完成但没有生成语音文件")
        return DownloadedMusic(path=destination, title=title, source_url=target_url)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def download_music_async(*args, **kwargs) -> DownloadedMusic:
    """Run the blocking downloader off the AgentService event loop."""

    return await asyncio.to_thread(download_music, *args, **kwargs)


def split_audio_for_qq_voice(
    source: Union[Path, str],
    output_dir: Union[Path, str],
    *,
    ffmpeg_path: str,
    preferred_prefix: str = "音乐",
    max_seconds: int = DEFAULT_VOICE_SEGMENT_SECONDS,
    timeout_seconds: float = 600,
) -> List[VoiceSegment]:
    """Convert audio into sequential MP3 chunks for OneBot ``record``.

    NapCat accepts common audio input for a ``record`` segment and performs
    its internal Silk conversion.  Keeping each chunk at 50 seconds avoids
    QQ's long-voice/file fallback while preserving music quality better than
    AMR-NB.  The returned paths are persistent because NapCat may still read
    them after the action response is received.
    """

    source_path = Path(source)
    if not source_path.is_file():
        raise MusicDownloadError("音频文件不存在：%s" % source_path)
    if not str(ffmpeg_path or "").strip():
        raise MusicDownloadError("未找到 ffmpeg，无法切分 QQ 语音")
    ffmpeg = str(ffmpeg_path)
    if not Path(ffmpeg).exists() and not shutil.which(ffmpeg):
        raise MusicDownloadError("未找到 ffmpeg：%s" % ffmpeg)
    try:
        segment_seconds = int(max_seconds)
    except (TypeError, ValueError) as exc:
        raise MusicDownloadError("QQ 语音分段时长必须是整数") from exc
    if segment_seconds <= 0 or segment_seconds > 300:
        raise MusicDownloadError("QQ 语音分段时长必须在 1 到 300 秒之间")

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    prefix = _clean_title(preferred_prefix or source_path.stem)
    file_prefix = prefix
    pattern = root / (file_prefix + ".part-%03d.mp3")
    # Avoid overwriting a previous request with the same title.
    if any(root.glob(prefix + ".part-*.mp3")):
        suffix = 2
        while any(root.glob("%s (%s).part-*.mp3" % (prefix, suffix))):
            suffix += 1
        file_prefix = "%s (%s)" % (prefix, suffix)
        pattern = root / (file_prefix + ".part-%03d.mp3")

    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "32k",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            "-segment_format",
            "mp3",
            str(pattern),
        ],
        timeout=timeout_seconds,
    )
    parts = sorted(root.glob(file_prefix + ".part-*.mp3"))
    if not parts:
        # The glob above intentionally stays narrow; inspect only files created
        # after this helper started rather than accidentally reusing old audio.
        raise MusicDownloadError("ffmpeg 切分完成但没有生成 QQ 语音片段")
    segments: List[VoiceSegment] = []
    for index, part in enumerate(parts, 1):
        if not part.is_file() or part.stat().st_size <= 0:
            continue
        segments.append(
            VoiceSegment(
                path=part,
                index=index,
                start_seconds=float((index - 1) * segment_seconds),
            )
        )
    if not segments:
        raise MusicDownloadError("ffmpeg 切分完成但 QQ 语音片段为空")
    return segments


__all__ = [
    "DownloadedMusic",
    "MusicDownloadError",
    "VoiceSegment",
    "DEFAULT_VOICE_SEGMENT_SECONDS",
    "download_music",
    "download_music_async",
    "split_audio_for_qq_voice",
]
