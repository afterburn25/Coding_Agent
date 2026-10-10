"""High-level media tools over FFmpeg/FFprobe.

The agent calls capabilities like ``extract_audio`` or ``trim_video`` and these
wrappers build safe, validated ffmpeg argv — users never write ffmpeg syntax.
When ffmpeg/ffprobe are missing the tools report TOOL_NOT_INSTALLED-friendly
errors so the router can fall back.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec

MAX_STDOUT = 20000
DEFAULT_TIMEOUT = 1800


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def _resolve(workspace: Path, raw: str, *, must_exist: bool = True) -> Path | None:
    p = (workspace / str(raw or "")).resolve()
    if not p.is_relative_to(workspace.resolve()):
        return None
    if must_exist and not p.exists():
        return None
    return p


def _run_ffmpeg(argv: list[str], workspace: Path, timeout: int) -> dict[str, Any]:
    exe = find_ffmpeg()
    if not exe:
        raise FileNotFoundError("ffmpeg is not installed (Tool Manager → install Gyan.FFmpeg)")
    started = time.time()
    try:
        proc = subprocess.run([exe, "-hide_banner", "-y", *argv], cwd=str(workspace),
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=no_window_flags(), encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ffmpeg timed out after {timeout}s")
    return {
        "command": " ".join(["ffmpeg", *argv]),
        "exit_code": proc.returncode,
        "elapsed_seconds": round(time.time() - started, 3),
        "stderr": (proc.stderr or "")[-MAX_STDOUT:],
        "ok": proc.returncode == 0,
    }


def probe_media(workspace: Path, raw: str) -> dict[str, Any]:
    exe = find_ffprobe()
    if not exe:
        raise FileNotFoundError("ffprobe is not installed (ships with FFmpeg)")
    src = _resolve(workspace, raw)
    if src is None:
        raise ValueError("file must exist inside the workspace")
    proc = subprocess.run([exe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(src)],
                          capture_output=True, text=True, timeout=60,
                          creationflags=no_window_flags(), encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe failed")[-300:])
    return json.loads(proc.stdout or "{}")


def build_extract_audio(src: Path, dst: Path) -> list[str]:
    return ["-i", str(src), "-vn", "-acodec", "mp3" if dst.suffix.lower() == ".mp3" else "pcm_s16le", str(dst)]


def build_trim(src: Path, dst: Path, start: float, duration: float) -> list[str]:
    argv = ["-i", str(src)]
    if start:
        argv += ["-ss", str(start)]
    if duration:
        argv += ["-t", str(duration)]
    argv += ["-c", "copy", str(dst)]
    return argv


def build_convert(src: Path, dst: Path, audio_bitrate: str = "") -> list[str]:
    argv = ["-i", str(src)]
    if audio_bitrate:
        argv += ["-b:a", audio_bitrate]
    argv.append(str(dst))
    return argv


def build_thumbnail(src: Path, dst: Path, at: float = 1.0) -> list[str]:
    return ["-i", str(src), "-ss", str(at), "-frames:v", "1", str(dst)]


def build_normalize(src: Path, dst: Path) -> list[str]:
    return ["-i", str(src), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", str(dst)]


def build_add_subtitles(src: Path, subs: Path, dst: Path, *, burn: bool = False) -> list[str]:
    if burn:
        safe = str(subs).replace("\\", "\\\\").replace(":", "\\:")
        return ["-i", str(src), "-vf", f"subtitles='{safe}'", str(dst)]
    return ["-i", str(src), "-i", str(subs), "-c", "copy", "-c:s", "mov_text", str(dst)]


def build_merge(files: list[Path], dst: Path) -> list[str]:
    argv: list[str] = []
    for f in files:
        argv += ["-i", str(f)]
    argv += ["-filter_complex", f"concat=n={len(files)}:v=1:a=1[outv][outa]", "-map", "[outv]", "-map", "[outa]", str(dst)]
    return argv


def default_whisper_model(workspace: Path) -> str | None:
    """Find a whisper GGML model under models/audio or models/."""
    for base in (workspace / "models" / "audio", workspace / "models"):
        if not base.is_dir():
            continue
        for candidate in sorted(base.glob("*.bin")) + sorted(base.glob("*.gguf")):
            if "whisper" in candidate.name.lower() or candidate.name.startswith("ggml-"):
                return str(candidate)
    return None


def default_voice_model(workspace: Path) -> str | None:
    """Find a piper voice .onnx under models/tts or models/."""
    for base in (workspace / "models" / "tts", workspace / "models"):
        if not base.is_dir():
            continue
        for candidate in sorted(base.rglob("*.onnx")):
            if candidate.with_suffix(".onnx.json").exists():
                return str(candidate)
    return None


def register_media_tools(registry: ToolRegistry, workspace: Path, *, jobs=None) -> None:

    def _src_dst(args: dict[str, Any]) -> tuple[Path, Path] | str:
        src = _resolve(workspace, str(args.get("source", "")))
        dst = _resolve(workspace, str(args.get("output", "")), must_exist=False)
        if src is None:
            return "ERROR: source must be an existing file inside the workspace"
        if dst is None:
            return "ERROR: output path must stay inside the workspace"
        return src, dst

    def wrap(builder) -> str:
        def call(args: dict[str, Any]) -> str:
            resolved = _src_dst(args)
            if isinstance(resolved, str):
                return resolved
            src, dst = resolved
            timeout = max(1, min(int(args.get("timeout_seconds", DEFAULT_TIMEOUT)), 3600))
            try:
                result = _run_ffmpeg(builder(src, dst, args), workspace, timeout)
            except (FileNotFoundError, ValueError, TimeoutError) as exc:
                return f"ERROR: {exc}"
            if not result["ok"]:
                return f"ERROR: ffmpeg exit {result['exit_code']}: {result['stderr'][-500:]}"
            return json.dumps({**result, "output": str(dst)}, ensure_ascii=False)
        return call

    def media_probe(args: dict[str, Any]) -> str:
        try:
            return json.dumps(probe_media(workspace, str(args["source"])), ensure_ascii=False)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            return f"ERROR: {exc}"

    registry.register(ToolSpec(
        "media_probe", "Inspect a media file (streams, format, duration) via ffprobe. Returns structured JSON metadata.",
        {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]},
        "shell.execute", media_probe,
        category="video", capabilities=["probe_media", "verify_media", "media_metadata"],
    ))
    registry.register(ToolSpec(
        "extract_audio", "Extract the audio track from a video/audio file (mp3 or wav by output extension).",
        {"type": "object", "properties": {"source": {"type": "string"}, "output": {"type": "string"}, "timeout_seconds": {"type": "integer"}}, "required": ["source", "output"]},
        "shell.execute", wrap(lambda s, d, a: build_extract_audio(s, d)),
        category="video", capabilities=["extract_audio"], requires_gpu=False,
    ))
    registry.register(ToolSpec(
        "trim_video", "Trim a media file by start/duration seconds (stream copy, fast).",
        {"type": "object", "properties": {"source": {"type": "string"}, "output": {"type": "string"}, "start": {"type": "number", "default": 0}, "duration": {"type": "number"}}, "required": ["source", "output"]},
        "shell.execute", wrap(lambda s, d, a: build_trim(s, d, float(a.get("start", 0)), float(a.get("duration", 0)))),
        category="video", capabilities=["trim_video"],
    ))
    registry.register(ToolSpec(
        "convert_video", "Convert a media file to another container/format (mp4, mkv, webm, mp3, wav...).",
        {"type": "object", "properties": {"source": {"type": "string"}, "output": {"type": "string"}, "audio_bitrate": {"type": "string"}}, "required": ["source", "output"]},
        "shell.execute", wrap(lambda s, d, a: build_convert(s, d, str(a.get("audio_bitrate", "")))),
        category="video", capabilities=["convert_video", "change_container"],
    ))
    registry.register(ToolSpec(
        "create_thumbnail", "Extract a single frame as an image thumbnail at a given timestamp.",
        {"type": "object", "properties": {"source": {"type": "string"}, "output": {"type": "string"}, "at": {"type": "number", "default": 1.0}}, "required": ["source", "output"]},
        "shell.execute", wrap(lambda s, d, a: build_thumbnail(s, d, float(a.get("at", 1.0)))),
        category="video", capabilities=["create_thumbnail", "extract_frames"],
    ))
    registry.register(ToolSpec(
        "normalize_audio", "Normalize audio loudness (EBU R128 loudnorm) for speech/video.",
        {"type": "object", "properties": {"source": {"type": "string"}, "output": {"type": "string"}}, "required": ["source", "output"]},
        "shell.execute", wrap(lambda s, d, a: build_normalize(s, d)),
        category="audio", capabilities=["normalize_audio"],
    ))
    def add_subtitles(args: dict[str, Any]) -> str:
        resolved = _src_dst(args)
        if isinstance(resolved, str):
            return resolved
        src, dst = resolved
        subs = _resolve(workspace, str(args.get("subtitles", "")))
        if subs is None:
            return "ERROR: subtitles file must exist inside the workspace"
        timeout = max(1, min(int(args.get("timeout_seconds", DEFAULT_TIMEOUT)), 3600))
        try:
            result = _run_ffmpeg(build_add_subtitles(src, subs, dst, burn=bool(args.get("burn", False))), workspace, timeout)
        except (FileNotFoundError, ValueError, TimeoutError) as exc:
            return f"ERROR: {exc}"
        if not result["ok"]:
            return f"ERROR: ffmpeg exit {result['exit_code']}: {result['stderr'][-500:]}"
        return json.dumps({**result, "output": str(dst)}, ensure_ascii=False)

    registry.register(ToolSpec(
        "add_subtitles", "Add a subtitle track to a video (soft mov_text mux) or burn subtitles in (burn=true).",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string"}, "subtitles": {"type": "string"}, "output": {"type": "string"},
                "burn": {"type": "boolean", "default": False},
            },
            "required": ["source", "subtitles", "output"],
        },
        "shell.execute", add_subtitles,
        category="video", capabilities=["add_subtitles"],
    ))

    def media_transcribe(args: dict[str, Any]) -> str:
        """Chain: extract audio (ffmpeg) -> whisper.cpp -> subtitles file."""
        src = _resolve(workspace, str(args.get("source", "")))
        if src is None:
            return "ERROR: source must be an existing file inside the workspace"
        model = str(args.get("model", "") or "") or (default_whisper_model(workspace) or "")
        if not model:
            return "ERROR: no whisper model — set 'model' or place a ggml-*.bin under models/audio/"
        model_path = Path(model)
        if not model_path.is_absolute():
            model_path = _resolve(workspace, model) or Path(model)
        if not model_path.is_file():
            return f"ERROR: whisper model not found: {model}"
        workdir = workspace / ".agent" / "media"
        workdir.mkdir(parents=True, exist_ok=True)
        audio = workdir / f"{src.stem}-{int(time.time())}.wav"
        job = jobs.submit("media", f"Transcribe {src.name}") if jobs is not None else None
        steps: list[dict[str, Any]] = []

        if job:
            jobs.update(job.id, state="running", detail="extracting audio")
        result = registry.execute("extract_audio", {
            "source": str(src.relative_to(workspace)),
            "output": str(audio.relative_to(workspace)),
        }, approved=bool(args.get("approved", False)))
        steps.append({"step": "extract_audio", "ok": not result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL"))})
        if not steps[-1]["ok"]:
            if job:
                jobs.update(job.id, state="failed", error=result[:300])
            return json.dumps({"ok": False, "failed_step": "extract_audio", "detail": result[:500], "steps": steps})

        if job:
            jobs.update(job.id, detail="transcribing (whisper.cpp)")
        whisper_args: list[str] = ["-l", str(args.get("language", "auto"))]
        result = registry.execute("whisper", {
            "model": str(model_path),
            "file": str(audio),
            "args": whisper_args,
        }, approved=bool(args.get("approved", False)))
        steps.append({"step": "transcribe", "ok": not result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL"))})
        if not steps[-1]["ok"]:
            if job:
                jobs.update(job.id, state="failed", error=result[:300])
            return json.dumps({"ok": False, "failed_step": "transcribe", "detail": result[:500],
                               "audio": str(audio), "steps": steps})
        srt = audio.with_suffix(".srt")
        out = {"ok": True, "steps": steps, "audio": str(audio),
               "transcript_json": str(audio.with_suffix(".json")), "subtitles": str(srt) if srt.exists() else "",
               "detail": result[:500]}
        if job:
            jobs.update(job.id, state="completed", detail=f"subtitles: {srt.name if srt.exists() else 'n/a'}")
        return json.dumps(out, ensure_ascii=False)

    registry.register(ToolSpec(
        "media_transcribe",
        "Media Agent recipe: extract audio from a video/audio file with FFmpeg, then transcribe with whisper.cpp to produce JSON + SRT subtitles. Chains through the Tool Registry — every step is permission-gated.",
        {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "video/audio file in the workspace"},
                "model": {"type": "string", "description": "whisper GGML model path (auto-detected under models/)"},
                "language": {"type": "string", "default": "auto"},
            },
            "required": ["source"],
        },
        "shell.execute",
        media_transcribe,
        category="audio",
        capabilities=["transcribe_video", "transcribe_audio", "generate_subtitles", "media_pipeline"],
    ))

    def speak_text(args: dict[str, Any]) -> str:
        """Text -> speech via the piper manifest tool (stdin-driven)."""
        text = str(args.get("text", ""))
        if not text.strip():
            return "ERROR: 'text' is required"
        model = str(args.get("model", "") or "") or (default_voice_model(workspace) or "")
        if not model:
            return "ERROR: no piper voice model — set 'model' or place a voice .onnx (+ .onnx.json) under models/tts/"
        model_path = Path(model)
        if not model_path.is_absolute():
            model_path = _resolve(workspace, model) or Path(model)
        if not model_path.is_file():
            return f"ERROR: voice model not found: {model}"
        out_rel = str(args.get("output", "") or f".agent/media/tts-{int(time.time())}.wav")
        out = _resolve(workspace, out_rel, must_exist=False)
        if out is None:
            return "ERROR: output must stay inside the workspace"
        out.parent.mkdir(parents=True, exist_ok=True)
        result = registry.execute("piper", {
            "text": text, "model": str(model_path), "output": str(out),
        }, approved=bool(args.get("approved", False)))
        if result.startswith(("ERROR", "PERMISSION", "TOOL_", "APPROVAL")):
            return result
        return json.dumps({"ok": True, "output": str(out), "characters": len(text), "detail": result[:300]},
                          ensure_ascii=False)

    registry.register(ToolSpec(
        "speak_text",
        "Generate speech from text with Piper TTS (delegates through the registry; auto-detects a voice .onnx under models/tts/). Outputs a WAV file.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "model": {"type": "string", "description": "piper voice .onnx path (auto-detected)"},
                "output": {"type": "string", "description": "output wav path (default .agent/media/tts-<ts>.wav)"},
            },
            "required": ["text"],
        },
        "shell.execute",
        speak_text,
        category="audio",
        capabilities=["speak_text", "generate_speech", "tts"],
    ))
