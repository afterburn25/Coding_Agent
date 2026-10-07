"""Chatterbox Turbo worker — runs inside the isolated voice runtime
(``runtime/voice/chatterbox`` venv), NOT inside the frozen Nexus backend.

Protocol: JSON lines on stdin, JSON lines on stdout. The backend
(``chatterbox.ChatterboxEngine``) owns this process lifecycle; everything
here is self-contained — no localcodeagent imports — so the script can
run from any interpreter that has chatterbox-tts installed.

    <- {"cmd": "ping"}
    -> {"ok": true, "pid": 1234}

    <- {"cmd": "load", "device": "auto", "model_dir": "...",
        "min_free_vram_mb": 1500}
    -> {"ok": true, "device": "cuda", "gpu": "...", "vram_alloc_mb": ...,
        "load_s": ...}

    <- {"cmd": "prepare_voice", "voice_id": "isabella",
        "reference": "path.wav", "norm_loudness": true}
    -> {"ok": true}

    <- {"cmd": "synthesize", "text": "...", "voice_id": "isabella",
        "temperature": 0.8, "top_p": 0.95, "top_k": 1000,
        "repetition_penalty": 1.2, "out": "path.wav"}
    -> {"ok": true, "sr": 24000, "seconds": ..., "gen_s": ..., "rtf": ...,
        "peak": ...}

    <- {"cmd": "capabilities"}   tokenizer-backed tag table
    <- {"cmd": "status"}         engine/device/metrics snapshot
    <- {"cmd": "unload"}         release model + VRAM without exiting
    <- {"cmd": "shutdown"}       clean exit

Failures answer {"ok": false, "error": "..."} — never an exception, so a
dying import or OOM reports diagnosably instead of hanging the queue.
"""
from __future__ import annotations

import sys as _sys

# The script's directory (localcodeagent/voice/) lands on sys.path when
# run as __main__ — types.py, engine.py etc. would then SHADOW stdlib
# modules for every import below (types → enum → re → json collapse).
# Remove it so only the venv's real stdlib + site-packages resolve.
import os.path as _osp
_here = _osp.dirname(_osp.abspath(__file__))
_sys.path = [p for p in _sys.path
             if _osp.abspath(p or ".") != _here]
del _sys, _osp, _here

import json
import os
import sys
import time
from pathlib import Path

MODEL_REPO = "ResembleAI/chatterbox-turbo"
MODEL_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
REQUIRED_FILES = (
    "ve.safetensors", "t3_turbo_v1.safetensors",
    "s3gen_meanflow.safetensors", "conds.pt",
    "tokenizer_config.json", "vocab.json", "merges.txt",
    "added_tokens.json", "special_tokens_map.json",
)

# Paralinguistic + delivery tokens this model's tokenizer advertises.
# Each still needs generation-level verification before Nexus relies on
# it — presence in the vocab is necessary, not sufficient.
KNOWN_TAGS = [
    "[laugh]", "[chuckle]", "[sigh]", "[gasp]", "[cough]", "[groan]",
    "[sniff]", "[shush]", "[clear throat]",
    "[happy]", "[angry]", "[fear]", "[surprised]", "[crying]",
    "[sarcastic]", "[dramatic]", "[narration]", "[whispering]",
    "[advertisement]",
]

_state = {
    "tts": None,
    "device": "",
    "model_dir": "",
    "load_s": 0.0,
    "voices": {},          # voice_id -> (ref_path, exaggeration, norm)
    "current_voice": "",   # t3 conds are global — must re-prepare on switch
    "synth_calls": 0,
    "synth_audio_s": 0.0,
    "synth_gen_s": 0.0,
}


def _out(**kw):
    sys.stdout.write(json.dumps(kw) + "\n")
    sys.stdout.flush()


def _err(msg: str, **kw):
    _out(ok=False, error=str(msg)[:600], **kw)


def _torch():
    import torch  # deferred — import cost + hard dep stay worker-side
    return torch


def _pick_device(req: str, min_free_vram_mb: float) -> tuple[str, dict]:
    """auto → cuda only when a device exists AND enough VRAM is free —
    the LLM/image lanes own the GPU first."""
    torch = _torch()
    info = {"cuda_available": bool(torch.cuda.is_available())}
    if req == "cpu" or not torch.cuda.is_available():
        return "cpu", info
    try:
        free_b, total_b = torch.cuda.mem_get_info(0)
        info["gpu"] = torch.cuda.get_device_name(0)
        info["vram_free_mb"] = round(free_b / 1e6)
        info["vram_total_mb"] = round(total_b / 1e6)
        if req == "cuda":
            return "cuda", info
        if free_b / 1e6 >= float(min_free_vram_mb):
            return "cuda", info
    except Exception:
        if req == "cuda":
            return "cuda", info
    return "cpu", info


# T3 (the 1.9 GB transformer) is the bulk of resident VRAM — bf16 halves it
# with no quality-relevant loss. s3gen/ve stay fp32: the vocoder path is
# where reduced precision is actually audible.
_DTYPES = {"bf16": "bfloat16", "fp16": "float16"}


def _cast_dtype(tts, dtype_name: str) -> None:
    if dtype_name not in _DTYPES:
        return
    torch = _torch()
    dt = getattr(torch, _DTYPES[dtype_name])
    tts.t3.to(dt)
    conds = getattr(tts, "conds", None)
    if conds is not None and hasattr(conds, "t3"):
        conds.t3 = conds.t3.to(dtype=dt)


def _cmd_load(req: dict) -> None:
    if _state["tts"] is not None:
        _out(ok=True, device=_state["device"], already=True,
             load_s=round(_state["load_s"], 2), sr=_state["tts"].sr,
             dtype=_state.get("dtype") or "fp32",
             tags=_supported_tags(_state["tts"]))
        return
    model_dir = Path(str(req.get("model_dir") or ""))
    missing = [f for f in REQUIRED_FILES if not (model_dir / f).exists()]
    if missing:
        _err(f"chatterbox model files missing: {', '.join(missing)}")
        return
    device, info = _pick_device(
        str(req.get("device") or "auto"),
        float(req.get("min_free_vram_mb") or 1500))
    dtype = str(req.get("dtype") or "").lower()
    if dtype not in _DTYPES:
        dtype = ""
    t0 = time.monotonic()
    try:
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        tts = ChatterboxTurboTTS.from_local(str(model_dir), device)
    except Exception as exc:
        _err(f"chatterbox-turbo load failed on {device}: {exc}", **info)
        return
    try:
        _cast_dtype(tts, dtype)
    except Exception:
        dtype = ""  # fp32 fallback — a failed cast must not lose the model
    _state.update(tts=tts, device=device, model_dir=str(model_dir),
                  load_s=time.monotonic() - t0, voices={},
                  current_voice="", dtype=dtype or "fp32")
    extra = {}
    try:
        torch = _torch()
        if device == "cuda":
            extra["vram_alloc_mb"] = round(torch.cuda.memory_allocated() / 1e6)
    except Exception:
        pass
    _out(ok=True, device=device, load_s=round(_state["load_s"], 2),
         sr=tts.sr, tags=_supported_tags(tts), **info, **extra)


def _supported_tags(tts) -> list[str]:
    """Tags the tokenizer encodes as dedicated single tokens."""
    out = []
    try:
        tok = tts.tokenizer
        vocab = tok.get_added_vocab()
        for tag in KNOWN_TAGS:
            ids = tok.encode(tag, add_special_tokens=False)
            if tag in vocab and len(ids) == 1 and ids[0] == vocab[tag]:
                out.append(tag.strip("[]"))
    except Exception:
        pass
    return out


def _conds_cache_path(ref: str, exag: float, norm: bool) -> Path | None:
    """Per-voice conditioning cache file, keyed on everything that makes
    the conds unique: reference audio content, exaggeration, loudness
    normalization, model dtype and revision. Lives beside the reference
    wav so each voice dir carries its own cache."""
    try:
        import hashlib
        digest = hashlib.sha256(Path(ref).read_bytes()).hexdigest()[:16]
        key = "-".join((
            digest, f"e{exag:.4f}", f"n{int(norm)}",
            str(_state.get("dtype") or "fp32"),
            MODEL_REVISION[:8]))
        return Path(ref).parent / f"conds-{key}.pt"
    except Exception:
        return None


def _apply_conditionals(tts, ref: str, exag: float, norm: bool) -> None:
    """Install per-voice conditioning — a torch.load from the on-disk
    cache when one exists (voice switch, worker restart after idle
    unload), else the full librosa/S3Gen/VE encode persisted for next
    time. A stale or unreadable cache always falls through to recompute.
    """
    cache = _conds_cache_path(ref, exag, norm)
    if cache is not None and cache.exists():
        try:
            from chatterbox.tts_turbo import Conditionals
            tts.conds = Conditionals.load(
                cache, map_location=getattr(tts, "device", "cpu"))
            return
        except Exception:
            pass
    tts.prepare_conditionals(ref, exaggeration=exag,
                             norm_loudness=norm)
    # prepare_conditionals rebuilds conds in fp32 — recast so the T3
    # conditioning matches the model dtype.
    conds = getattr(tts, "conds", None)
    if conds is not None and hasattr(conds, "t3"):
        dt = _DTYPES.get(str(_state.get("dtype") or ""))
        if dt:
            conds.t3 = conds.t3.to(dtype=getattr(_torch(), dt))
    if cache is not None and conds is not None:
        try:
            conds.save(cache)
            # One cache file per voice dir — a changed reference or
            # param set leaves the old key orphaned.
            for stale in cache.parent.glob("conds-*.pt"):
                if stale != cache:
                    stale.unlink(missing_ok=True)
        except Exception:
            pass


def _cmd_prepare_voice(req: dict) -> None:
    tts = _state["tts"]
    if tts is None:
        _err("engine not loaded")
        return
    voice_id = str(req.get("voice_id") or "default")
    ref = str(req.get("reference") or "")
    if not ref or not Path(ref).exists():
        _err(f"reference audio not found: {ref!r}")
        return
    exag = float(req.get("exaggeration") or 0.5)
    norm = bool(req.get("norm_loudness", True))
    try:
        _apply_conditionals(tts, ref, exag, norm)
    except AssertionError as exc:
        _err(f"reference rejected: {exc}")
        return
    except Exception as exc:
        _err(f"prepare_conditionals failed: {exc}")
        return
    _state["voices"][voice_id] = (ref, exag, norm)
    _state["current_voice"] = voice_id
    _out(ok=True, voice_id=voice_id)


def _cmd_synthesize(req: dict) -> None:
    tts = _state["tts"]
    if tts is None:
        _err("engine not loaded")
        return
    text = str(req.get("text") or "").strip()
    if not text:
        _err("empty text")
        return
    out = str(req.get("out") or "")
    if not out:
        _err("missing out path")
        return
    voice_id = str(req.get("voice_id") or "")
    if voice_id and voice_id != _state["current_voice"]:
        # t3 conds are shared state on the model — a different voice
        # must re-prepare or we'd synthesize with the last voice's
        # conditioning.
        spec = _state["voices"].get(voice_id)
        if spec is None:
            _err(f"voice {voice_id!r} not prepared")
            return
        try:
            _apply_conditionals(tts, spec[0], spec[1], spec[2])
            _state["current_voice"] = voice_id
        except Exception as exc:
            _err(f"voice switch to {voice_id!r} failed: {exc}")
            return
    try:
        t0 = time.monotonic()
        wav = tts.generate(
            text,
            temperature=float(req.get("temperature") or 0.8),
            top_p=float(req.get("top_p") or 0.95),
            top_k=int(req.get("top_k") or 1000),
            repetition_penalty=float(req.get("repetition_penalty") or 1.2),
        )
        gen_s = time.monotonic() - t0
        import torchaudio
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        # PCM_S16 — the backend reads with stdlib `wave`, which rejects
        # torchaudio's default float32 (format tag 3).
        torchaudio.save(out, wav.clamp(-1.0, 1.0), tts.sr,
                        encoding="PCM_S", bits_per_sample=16)
        seconds = wav.shape[-1] / tts.sr
        _state["synth_calls"] += 1
        _state["synth_audio_s"] += seconds
        _state["synth_gen_s"] += gen_s
        _out(ok=True, sr=tts.sr, seconds=round(seconds, 3),
             gen_s=round(gen_s, 3),
             rtf=round(gen_s / max(seconds, 1e-6), 3),
             peak=round(float(wav.abs().max()), 4))
    except Exception as exc:
        _err(f"generation failed: {exc}")


def _cmd_capabilities() -> None:
    """Tokenizer-backed tag support: a tag only counts when it encodes to
    its dedicated single token id — multi-piece tokenization means the
    runtime would read it as literal text."""
    tts = _state["tts"]
    supported = set(_supported_tags(tts)) if tts is not None else set()
    tags = {tag.strip("[]"): tag.strip("[]") in supported
            for tag in KNOWN_TAGS}
    _out(ok=True, engine="chatterbox-turbo", model=MODEL_REPO,
         revision=MODEL_REVISION, tags=tags,
         loaded=tts is not None)


def _cmd_status() -> None:
    extra = {}
    try:
        torch = _torch()
        if _state["device"] == "cuda" and torch.cuda.is_available():
            extra["vram_alloc_mb"] = round(
                torch.cuda.memory_allocated() / 1e6)
    except Exception:
        pass
    gen = _state["synth_gen_s"]
    _out(ok=True, loaded=_state["tts"] is not None,
         device=_state["device"], load_s=round(_state["load_s"], 2),
         voices=sorted(_state["voices"]),
         synth_calls=_state["synth_calls"],
         synth_audio_s=round(_state["synth_audio_s"], 2),
         synth_gen_s=round(gen, 2),
         rtf=round(gen / _state["synth_audio_s"], 3)
         if _state["synth_audio_s"] else None,
         **extra)


def _cmd_unload() -> None:
    import gc
    _state["tts"] = None
    _state["voices"] = {}
    _state["current_voice"] = ""
    gc.collect()
    try:
        torch = _torch()
        if _state["device"] == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    _state["device"] = ""
    _out(ok=True)


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _err("bad json")
            continue
        cmd = str(req.get("cmd") or "")
        if cmd == "ping":
            _out(ok=True, pid=os.getpid())
        elif cmd == "load":
            _cmd_load(req)
        elif cmd == "prepare_voice":
            _cmd_prepare_voice(req)
        elif cmd == "synthesize":
            _cmd_synthesize(req)
        elif cmd == "capabilities":
            _cmd_capabilities()
        elif cmd == "status":
            _cmd_status()
        elif cmd == "unload":
            _cmd_unload()
        elif cmd == "shutdown":
            _out(ok=True)
            return 0
        else:
            _err(f"unknown cmd: {cmd!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
