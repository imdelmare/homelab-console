from io import BytesIO
import wave

import httpx
import pytest

from app.services.ollama_media import MediaAnalysisError, _analyze_media, _audio_to_wav, _is_supported_image


def _silent_wav() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\x00\x00" * 800)
    return output.getvalue()


def test_audio_conversion_produces_16khz_mono_pcm_wav():
    converted = _audio_to_wav(_silent_wav())

    with wave.open(BytesIO(converted), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000


def test_invalid_audio_is_rejected():
    with pytest.raises(MediaAnalysisError, match="invalid audio"):
        _audio_to_wav(b"not audio")


def test_supported_image_magic_is_bounded_to_known_formats():
    assert _is_supported_image(b"\xff\xd8\xffjpeg")
    assert _is_supported_image(b"\x89PNG\r\n\x1a\npng")
    assert _is_supported_image(b"RIFF\x00\x00\x00\x00WEBPdata")
    assert not _is_supported_image(b"GIF89a")


@pytest.mark.parametrize(
    ("media_kind", "expected_instruction", "model_output"),
    [
        ("image", "esclusivamente in italiano", "Un gatto arancione."),
        ("audio", "lingua realmente parlata", "The lab is stable."),
    ],
)
async def test_media_language_policy_is_preserved(
    monkeypatch,
    media_kind,
    expected_instruction,
    model_output,
):
    class Client:
        captured = None

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, **kwargs):
            type(self).captured = kwargs["json"]
            return httpx.Response(
                200,
                json={"message": {"content": model_output}},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr("app.services.ollama_media.httpx.AsyncClient", Client)

    result = await _analyze_media(b"bounded-media", "operator prompt", media_kind=media_kind)

    assert Client.captured is not None
    assert expected_instruction in Client.captured["messages"][0]["content"]
    assert result == model_output
