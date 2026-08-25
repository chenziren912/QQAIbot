from __future__ import annotations

from pathlib import Path

from app.service import _find_local_executable


def test_ffmpeg_path_override_works_without_interactive_path(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"stub")
    monkeypatch.setenv("FFMPEG_PATH", str(executable))
    monkeypatch.setattr("app.service.shutil.which", lambda *_: None)
    assert _find_local_executable("ffmpeg") == str(executable)
