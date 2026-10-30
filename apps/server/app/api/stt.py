"""Speech-to-text router — fully local, no Google dependency.

Why a backend FastAPI endpoint instead of browser whisper.cpp WASM:
  * The user's network cannot reach Google, so `webkitSpeechRecognition` on
    Chrome/Edge streams audio to Google and dies.
  * The planned `whisper-web` npm package is gone (404), and the official
    whisper.cpp WASM prebuilt artifacts are not reachable (HF Space 401 /
    GitHub raw blocked), so there is no practical browser-side WASM path.
  * `faster-whisper` (CT2 backend, int8 quantized — the tiny.en equivalent of
    the "Q4_0" idea) is ALREADY installed in this venv. It runs 100% locally
    on the machine hosting the FastAPI server, which is exactly the demo
    story we want: no cloud STT, no mic audio leaves the building.

The model (`Systran/faster-whisper-tiny.en`, ~75MB) downloads from
huggingface.co once on first request (HF is reachable on this network) and is
then cached on disk for all subsequent runs. For offline venues we also bundle a
pre-downloaded copy at `apps/server/models/faster-whisper-tiny.en`: if that
directory contains `model.bin` it is used directly with ZERO network access
(see `_resolve_model_path`). Override the location with `STT_MODEL_DIR`.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter(prefix="/stt", tags=["stt"])

logger = logging.getLogger(__name__)

# Lazy singleton so the model loads only when voice input is actually used.
_model: Any | None = None
_model_load_error: str = ""

# Config — overridable via env for hackathon venues that need a different
# huggingface mirror (e.g. HF_ENDPOINT=https://hf-mirror.com).
_MODEL_NAME = "Systran/faster-whisper-tiny.en"
_MODEL_DEVICE = "cpu"
_MODEL_COMPUTE_TYPE = "int8"
_MODEL_LANGUAGE = "en"
_SAMPLE_RATE = 16000

# Pre-downloaded local copy shipped with the repo for offline venues
# (apps/server/models/faster-whisper-tiny.en). Override with STT_MODEL_DIR.
_LOCAL_MODEL_DIR = str(Path(__file__).resolve().parents[2] / "models" / "faster-whisper-tiny.en")


def _local_model_ready() -> bool:
    """True when a usable pre-downloaded model copy exists on disk.

    Checks STT_MODEL_DIR (if set) or the bundled copy under apps/server/models.
    Used to (a) pick the fully-offline load path and (b) self-heal a stale cached
    load error after the model finishes downloading.
    """
    env_dir = os.environ.get("STT_MODEL_DIR", "")
    local_dir = env_dir or _LOCAL_MODEL_DIR
    return bool(local_dir) and os.path.isdir(local_dir) and os.path.exists(os.path.join(local_dir, "model.bin"))


class SttResponse(BaseModel):
    text: str
    language: str = _MODEL_LANGUAGE
    source: str = "faster-whisper"
    model: str = _MODEL_NAME
    duration_seconds: float = 0.0


class SttStatusResponse(BaseModel):
    enabled: bool
    source: str = "faster-whisper"
    model: str = _MODEL_NAME
    detail: str = ""


def _load_model() -> Any:
    """Return the shared WhisperModel singleton, loading it on first use.

    Download/load failures raise RuntimeError with a user-friendly message;
    the caller maps it to a 503 so the frontend can fall back to typing.
    """
    global _model, _model_load_error
    if _model is not None:
        return _model
    # Self-heal: `_model_load_error` is cached at process level. If the very first
    # load failed while HuggingFace was unreachable (e.g. SSL CERTIFICATE_VERIFY_
    # FAILED) but the bundled local copy has since appeared on disk, clear the
    # stale error and retry from the local path — otherwise the server would keep
    # throwing the cached error forever even after the model is downloaded.
    if _model_load_error and _local_model_ready():
        logger.info("STT: clearing stale load error; local model copy is now present")
        _model_load_error = ""
    if _model_load_error:
        raise RuntimeError(_model_load_error)
    try:
        from faster_whisper import WhisperModel

        model_path, download_root = _resolve_model_path()
        using_local = model_path != _MODEL_NAME
        _model = WhisperModel(
            model_path,
            device=_MODEL_DEVICE,
            compute_type=_MODEL_COMPUTE_TYPE,
            download_root=download_root,
            # When a local copy exists we force a FULLY offline load: faster-whisper
            # must never fall back to the HF hub (some venues throw ConnectError:
            # [SSL: CERTIFICATE_VERIFY_FAILED] even when a proxy is reachable).
            local_files_only=using_local,
        )
        return _model
    except Exception as exc:  # network / missing ctranslate2 / OOM …
        _model_load_error = f"Whisper model unavailable: {exc}"
        logger.warning("STT model load failed: %s", exc)
        raise RuntimeError(_model_load_error) from exc


def _resolve_model_path() -> tuple[str, str]:
    """Return (model_path, download_root).

    If a local model directory (STT_MODEL_DIR or the bundled copy) already
    contains model.bin, faster-whisper loads it directly with NO network access —
    that is the offline hackathon path. Otherwise fall back to HF: model name +
    STT_MODEL_DIR (or the default HF cache) as download root.
    """
    if _local_model_ready():
        env_dir = os.environ.get("STT_MODEL_DIR", "")
        local_dir = env_dir or _LOCAL_MODEL_DIR
        return local_dir, os.path.dirname(local_dir)
    env_dir = os.environ.get("STT_MODEL_DIR", "")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return _MODEL_NAME, env_dir
    return _MODEL_NAME, ""


@router.get("/status", response_model=SttStatusResponse)
def stt_status() -> SttStatusResponse:
    """Lightweight availability probe the frontend calls once on mount.

    We intentionally do NOT force the model download here (75MB on first call
    would make the browser hang); we just answer whether the environment looks
    usable. The real gate is the transcribe call itself.
    """
    try:
        import importlib.util

        has_faster = importlib.util.find_spec("faster_whisper") is not None
        if not has_faster:
            return SttStatusResponse(
                enabled=False,
                detail="faster-whisper is not installed in the server environment.",
            )
        if _model_load_error and not _local_model_ready():
            # Cached error AND still no local copy to self-heal from → report it.
            # _load_model() clears the error as soon as a local copy exists.
            return SttStatusResponse(
                enabled=False,
                detail=(
                    f"{_model_load_error} — no local model at "
                    f"{_LOCAL_MODEL_DIR}. Download it once (or set STT_MODEL_DIR "
                    f"to an existing copy); see the server README."
                ),
            )
        return SttStatusResponse(enabled=True)
    except Exception as exc:  # pragma: no cover
        return SttStatusResponse(enabled=False, detail=f"Status check failed: {exc}")
@router.post("/transcribe", response_model=SttResponse)
async def stt_transcribe(upload: UploadFile) -> SttResponse:
    """Transcribe a 16-bit 16 kHz mono WAV uploaded by the browser.

    The mic never touches Google: audio goes browser -> our FastAPI server
    -> local whisper (tiny.en, int8) -> text. Returns clean text for the
    agent input box.
    """
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio upload.")
    audio, duration = _decode_wav(raw)
    if len(audio) < 0.1 * _SAMPLE_RATE:
        raise HTTPException(
            status_code=400,
            detail="Audio is too short to transcribe (speak for at least ~1 second).",
        )

    try:
        model = _load_model()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    def _run_transcribe(m: Any, samples: np.ndarray) -> tuple[str, Any]:
        # Run the whole generator INSIDE the worker thread — otherwise the
        # `join()` below would iterate segments on the event loop and block it.
        segs, info = m.transcribe(
            samples,
            language=_MODEL_LANGUAGE,
            beam_size=5,
            vad_filter=False,
        )
        return "".join(seg.text for seg in segs).strip(), info

    try:
        text, info = await asyncio.to_thread(_run_transcribe, model, audio.astype(np.float32))
    except Exception as exc:  # pragma: no cover — inference errors surface as 500
        logger.warning("STT inference failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}") from exc

    duration = getattr(info, "duration", duration)
    return SttResponse(text=text, duration_seconds=duration or 0.0)


def _decode_wav(raw: bytes) -> tuple[np.ndarray, float]:
    """Decode the browser WAV into a 16 kHz mono float32 array.

    The hook is allowed to record at the device's native rate then re-encode
    to 16 kHz, so we defensively handle both 16 kHz and other rates.
    """
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            rate = wav.getframerate()
            channels = wav.getnchannels()
            n = wav.getnframes()
            pcm = np.frombuffer(wav.readframes(n), dtype=np.int16)
    except (wave.Error, EOFError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid WAV audio: {exc}") from exc

    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
    audio = pcm.astype(np.float32) / 32768.0

    if rate != _SAMPLE_RATE:
        # Linear resample — adequate for a demo voice input; the model is robust
        # to ±10% sample-rate drift and this is only a fallback path anyway.
        old_n = audio.shape[0]
        new_n = int(round(old_n * _SAMPLE_RATE / rate))
        audio = np.interp(
            np.linspace(0, old_n - 1, new_n),
            np.arange(old_n),
            audio,
        ).astype(np.float32)

    duration = audio.shape[0] / _SAMPLE_RATE
    return audio, duration