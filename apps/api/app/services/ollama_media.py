"""Bounded local-only analysis for Telegram photos and voice messages."""

from __future__ import annotations

import base64
from io import BytesIO

import av
import httpx

from app.core.settings import get_settings


class MediaAnalysisError(Exception):
    pass


async def analyze_image(data: bytes, prompt: str) -> str:
    if not get_settings().conversation_enabled:
        raise MediaAnalysisError("conversation service is disabled")
    if not _is_supported_image(data):
        raise MediaAnalysisError("unsupported image format")
    return await _analyze_media(
        data,
        prompt or "Descrivi brevemente questa immagine.",
        media_kind="image",
    )


async def analyze_audio(data: bytes, prompt: str) -> str:
    if not get_settings().conversation_enabled:
        raise MediaAnalysisError("conversation service is disabled")
    wav = _audio_to_wav(data)
    return await _analyze_media(
        wav,
        prompt or "Trascrivi questo audio. Restituisci solo ciò che viene detto.",
        media_kind="audio",
    )


async def _analyze_media(data: bytes, prompt: str, *, media_kind: str) -> str:
    settings = get_settings()
    if not settings.ollama_base_url or not settings.ollama_model:
        raise MediaAnalysisError("local media model is not configured")

    payload = {
        "model": settings.ollama_model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Analizza l'immagine e rispondi esclusivamente in italiano, "
                    "in modo preciso e conciso."
                    if media_kind == "image"
                    else
                    "Trascrivi fedelmente le parole nella lingua realmente parlata. "
                    "Non aggiungere introduzioni, spiegazioni o traduzioni."
                ),
            },
            {
                "role": "user",
                "content": prompt[:1000],
                # Ollama 0.31 / Gemma 4 detects WAV and image formats by magic
                # bytes through the established multimodal images field.
                "images": [base64.b64encode(data).decode("ascii")],
            }
        ],
        "stream": False,
        "think": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": {
            "temperature": 0,
            "num_ctx": 8192,
            "num_predict": 600,
        },
    }
    timeout = httpx.Timeout(
        settings.ollama_timeout_seconds,
        connect=settings.ollama_connect_timeout_seconds,
    )
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{settings.ollama_base_url.rstrip('/')}/api/chat",
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise MediaAnalysisError("local media model timed out") from exc
    except httpx.HTTPError as exc:
        raise MediaAnalysisError("local media model request failed") from exc

    if response.status_code >= 400:
        raise MediaAnalysisError(f"local media model returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise MediaAnalysisError("local media model returned an invalid response") from exc
    message = body.get("message") if isinstance(body, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise MediaAnalysisError("local media model returned an empty response")
    return content.strip()[:4000]


def _audio_to_wav(data: bytes) -> bytes:
    try:
        source = av.open(BytesIO(data), mode="r")
        output_bytes = BytesIO()
        output = av.open(output_bytes, mode="w", format="wav")
        stream = output.add_stream("pcm_s16le", rate=16000)
        stream.layout = "mono"
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in source.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in stream.encode(converted):
                    output.mux(packet)
        for converted in resampler.resample(None):
            for packet in stream.encode(converted):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)
        output.close()
        source.close()
    except (av.error.FFmpegError, EOFError, ValueError) as exc:  # type: ignore
        raise MediaAnalysisError("unsupported or invalid audio") from exc
    wav = output_bytes.getvalue()
    if not wav.startswith(b"RIFF") or b"WAVE" not in wav[:16]:
        raise MediaAnalysisError("audio conversion failed")
    return wav


def _is_supported_image(data: bytes) -> bool:
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
    )
