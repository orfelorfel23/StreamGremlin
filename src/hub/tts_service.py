"""
StreamGremlin Modular TTS Service
Standard-Engine: Microsoft Azure Neural Voices (`edge-tts`) - kostenlos, ohne API-Key.
Vorbereitet für zukünftige lokale Engines (Piper / Kokoro / Voice Cloning).
"""
import hashlib
import logging
from pathlib import Path
from typing import Protocol
import edge_tts

logger = logging.getLogger("streamgremlin.tts")


class TTSBackend(Protocol):
    async def synthesize(self, text: str, voice: str, output_path: Path) -> bool:
        ...


class EdgeTTSBackend:
    """Microsoft Azure Neural TTS via edge-tts."""

    async def synthesize(self, text: str, voice: str, output_path: Path) -> bool:
        try:
            communicate = edge_tts.Communicate(text=text, voice=voice)
            await communicate.save(str(output_path))
            return output_path.exists() and output_path.stat().st_size > 0
        except Exception as exc:
            logger.warning("edge-tts synthesis failed (%s): %s", voice, exc)
            return False


class PiperPlaceholderBackend:
    """Modulare Schnittstelle für spätere lokale Piper-ONNX-Modelle oder Voice-Cloning."""

    async def synthesize(self, text: str, voice: str, output_path: Path) -> bool:
        logger.info("Local Piper/Cloning backend placeholder invoked for '%s'", text)
        return False


class TTSService:
    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.backends: dict[str, TTSBackend] = {
            "edge-tts": EdgeTTSBackend(),
            "piper": PiperPlaceholderBackend(),
        }

    async def generate_audio(
        self,
        text: str,
        voice: str = "de-DE-KatjaNeural",
        max_chars: int = 200,
        engine: str = "edge-tts",
    ) -> dict[str, str | bool]:
        cleaned = " ".join(text.strip().split())[:max_chars]
        if not cleaned:
            return {"ok": False, "text": "", "audio_url": ""}

        digest = hashlib.sha256(f"{engine}:{voice}:{cleaned}".encode("utf-8")).hexdigest()[:16]
        filename = f"tts_{digest}.mp3"
        out_path = self.cache_dir / filename

        if out_path.exists() and out_path.stat().st_size > 0:
            return {
                "ok": True,
                "text": cleaned,
                "voice": voice,
                "audio_url": f"/api/tts/cache/{filename}",
                "fallback_browser": False,
            }

        backend = self.backends.get(engine, self.backends["edge-tts"])
        success = await backend.synthesize(cleaned, voice, out_path)
        if success:
            return {
                "ok": True,
                "text": cleaned,
                "voice": voice,
                "audio_url": f"/api/tts/cache/{filename}",
                "fallback_browser": False,
            }

        # Fallback to browser Web Speech API in OBS Overlay if offline
        return {
            "ok": True,
            "text": cleaned,
            "voice": voice,
            "audio_url": "",
            "fallback_browser": True,
        }
