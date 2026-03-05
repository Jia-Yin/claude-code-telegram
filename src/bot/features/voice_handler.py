"""Handle voice message transcription via ElevenLabs, Mistral, or OpenAI."""

from dataclasses import dataclass
from datetime import timedelta
from io import BytesIO
from typing import Any, Optional

import structlog
from telegram import Voice

from src.config.settings import Settings

logger = structlog.get_logger(__name__)


@dataclass
class ProcessedVoice:
    """Result of voice message processing."""

    prompt: str
    transcription: str
    duration: int


class VoiceHandler:
    """Transcribe Telegram voice messages using ElevenLabs, Mistral, or OpenAI."""

    def __init__(self, config: Settings):
        self.config = config
        self._elevenlabs_client: Optional[Any] = None
        self._mistral_client: Optional[Any] = None
        self._openai_client: Optional[Any] = None

    def _ensure_allowed_file_size(self, file_size: Optional[int]) -> None:
        """Reject files that exceed the configured max size."""
        if (
            isinstance(file_size, int)
            and file_size > self.config.voice_max_file_size_bytes
        ):
            raise ValueError(
                "Voice message too large "
                f"({file_size / 1024 / 1024:.1f}MB). "
                f"Max allowed: {self.config.voice_max_file_size_mb}MB. "
                "Adjust VOICE_MAX_FILE_SIZE_MB if needed."
            )

    async def process_voice_message(
        self, voice: Voice, caption: Optional[str] = None
    ) -> ProcessedVoice:
        """Download and transcribe a voice message.

        1. Download .ogg bytes from Telegram
        2. Call the configured transcription API (Mistral or OpenAI)
        3. Build a prompt combining caption + transcription
        """
        initial_file_size = getattr(voice, "file_size", None)
        self._ensure_allowed_file_size(initial_file_size)

        # Resolve Telegram file metadata before downloading bytes.
        file = await voice.get_file()
        resolved_file_size = getattr(file, "file_size", None)
        self._ensure_allowed_file_size(resolved_file_size)

        # Refuse unknown-size payloads to avoid unbounded downloads.
        if not isinstance(initial_file_size, int) and not isinstance(
            resolved_file_size, int
        ):
            raise ValueError(
                "Unable to determine voice message size before download. "
                "Please retry with a smaller voice message."
            )

        # Download voice data
        voice_bytes = bytes(await file.download_as_bytearray())
        self._ensure_allowed_file_size(len(voice_bytes))

        logger.info(
            "Transcribing voice message",
            provider=self.config.voice_provider,
            duration=voice.duration,
            file_size=initial_file_size or resolved_file_size or len(voice_bytes),
        )

        if self.config.voice_provider == "elevenlabs":
            transcription = await self._transcribe_elevenlabs(voice_bytes)
        elif self.config.voice_provider == "openai":
            transcription = await self._transcribe_openai(voice_bytes)
        else:
            transcription = await self._transcribe_mistral(voice_bytes)

        logger.info(
            "Voice transcription complete",
            transcription_length=len(transcription),
            duration=voice.duration,
        )

        # Build prompt
        label = caption if caption else "Voice message transcription:"
        prompt = f"{label}\n\n{transcription}"

        dur = voice.duration
        duration_secs = int(dur.total_seconds()) if isinstance(dur, timedelta) else dur

        return ProcessedVoice(
            prompt=prompt,
            transcription=transcription,
            duration=duration_secs,
        )

    async def _transcribe_elevenlabs(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the ElevenLabs Speech-to-Text API."""
        client = self._get_elevenlabs_client()
        audio_file = BytesIO(voice_bytes)
        audio_file.name = "voice.ogg"

        try:
            response = await client.speech_to_text.convert(
                file=audio_file,
                model_id=self.config.resolved_voice_model,
            )
        except Exception as exc:
            logger.warning(
                "ElevenLabs transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("ElevenLabs transcription request failed.") from exc

        text = self._extract_elevenlabs_text(response)
        if not text:
            raise ValueError("ElevenLabs transcription returned an empty response.")
        return text

    @staticmethod
    def _extract_elevenlabs_text(response: Any) -> str:
        """Extract transcript text from ElevenLabs SDK response variants."""
        text_val = getattr(response, "text", "")
        text = text_val.strip() if isinstance(text_val, str) else ""
        if text:
            return text

        transcript_val = getattr(response, "transcript", "")
        transcript = transcript_val.strip() if isinstance(transcript_val, str) else ""
        if transcript:
            return transcript

        transcripts = getattr(response, "transcripts", None)
        if isinstance(transcripts, list):
            parts = []
            for item in transcripts:
                part_val = getattr(item, "text", "")
                part = part_val.strip() if isinstance(part_val, str) else ""
                if part:
                    parts.append(part)
            if parts:
                return "\n".join(parts)

        return ""

    def _get_elevenlabs_client(self) -> Any:
        """Create and cache an ElevenLabs async client on first use."""
        if self._elevenlabs_client is not None:
            return self._elevenlabs_client

        try:
            from elevenlabs.client import AsyncElevenLabs
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'elevenlabs' is missing for voice transcription. "
                "Install voice extras: "
                'pip install "claude-code-telegram[voice]"'
            ) from exc

        api_key = self.config.elevenlabs_api_key_str
        if not api_key:
            raise RuntimeError("ElevenLabs API key is not configured.")

        self._elevenlabs_client = AsyncElevenLabs(api_key=api_key)
        return self._elevenlabs_client

    async def _transcribe_mistral(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the Mistral API (Voxtral)."""
        client = self._get_mistral_client()
        try:
            response = await client.audio.transcriptions.complete_async(
                model=self.config.resolved_voice_model,
                file={
                    "content": voice_bytes,
                    "file_name": "voice.ogg",
                },
            )
        except Exception as exc:
            logger.warning(
                "Mistral transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("Mistral transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("Mistral transcription returned an empty response.")
        return text

    def _get_mistral_client(self) -> Any:
        """Create and cache a Mistral client on first use."""
        if self._mistral_client is not None:
            return self._mistral_client

        try:
            from mistralai import Mistral
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'mistralai' is missing for voice transcription. "
                "Install voice extras: "
                'pip install "claude-code-telegram[voice]"'
            ) from exc

        api_key = self.config.mistral_api_key_str
        if not api_key:
            raise RuntimeError("Mistral API key is not configured.")

        self._mistral_client = Mistral(api_key=api_key)
        return self._mistral_client

    async def _transcribe_openai(self, voice_bytes: bytes) -> str:
        """Transcribe audio using the OpenAI Whisper API."""
        client = self._get_openai_client()
        try:
            response = await client.audio.transcriptions.create(
                model=self.config.resolved_voice_model,
                file=("voice.ogg", voice_bytes),
            )
        except Exception as exc:
            logger.warning(
                "OpenAI transcription request failed",
                error_type=type(exc).__name__,
            )
            raise RuntimeError("OpenAI transcription request failed.") from exc

        text = (getattr(response, "text", "") or "").strip()
        if not text:
            raise ValueError("OpenAI transcription returned an empty response.")
        return text

    def _get_openai_client(self) -> Any:
        """Create and cache an OpenAI client on first use."""
        if self._openai_client is not None:
            return self._openai_client

        try:
            from openai import AsyncOpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Optional dependency 'openai' is missing for voice transcription. "
                "Install voice extras: "
                'pip install "claude-code-telegram[voice]"'
            ) from exc

        api_key = self.config.openai_api_key_str
        if not api_key:
            raise RuntimeError("OpenAI API key is not configured.")

        self._openai_client = AsyncOpenAI(api_key=api_key)
        return self._openai_client
