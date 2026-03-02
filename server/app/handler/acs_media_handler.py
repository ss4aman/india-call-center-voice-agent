"""Handles media streaming to Azure Voice Live API via WebSocket."""

import asyncio
import base64
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import numpy as np
from azure.identity.aio import ManagedIdentityCredential
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Data

from .ambient_mixer import AmbientMixer

logger = logging.getLogger(__name__)

# Default chunk size in bytes (100ms of audio at 24kHz, 16-bit mono)
DEFAULT_CHUNK_SIZE = 4800  # 24000 samples/sec * 0.1 sec * 2 bytes


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float for %s=%s. Using default=%s", name, value, default)
        return default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid int for %s=%s. Using default=%s", name, value, default)
        return default


def _load_puri_bank_mock_db() -> dict:
    configured_path = os.getenv("PURI_BANK_DATA_FILE", "").strip()
    candidate_paths = []

    if configured_path:
        candidate_paths.append(Path(configured_path))

    candidate_paths.extend(
        [
            Path(__file__).resolve().parents[1] / "data" / "puri_bank_mock_accounts.json",
            Path(__file__).resolve().parents[2] / "app" / "data" / "puri_bank_mock_accounts.json",
            Path.cwd() / "app" / "data" / "puri_bank_mock_accounts.json",
        ]
    )

    for data_file in candidate_paths:
        try:
            if data_file.exists():
                with data_file.open("r", encoding="utf-8") as file:
                    bank_data = json.load(file)
                accounts = bank_data.get("accounts", [])
                logger.info(
                    "Loaded Puri Bank mock data from %s with %s accounts",
                    str(data_file),
                    len(accounts),
                )
                return bank_data
        except Exception as error:
            logger.warning("Failed reading mock data file %s: %s", str(data_file), error)

    logger.error("Puri Bank mock data file not found. Checked paths: %s", [str(path) for path in candidate_paths])
    return {"bankName": "Puri Bank", "currency": "INR", "accounts": []}


def _build_puri_bank_instructions() -> str:
    bank_data = _load_puri_bank_mock_db()
    bank_name = bank_data.get("bankName", "Puri Bank")
    currency = bank_data.get("currency", "INR")
    accounts = bank_data.get("accounts", [])

    account_lines = []
    for account in accounts:
        account_lines.append(
            (
                f"accountId={account.get('accountId')}, customerName={account.get('customerName')}, "
                f"mobileLast4={account.get('registeredMobileLast4')}, dobDayMonth={account.get('dobDayMonth')}, "
                f"accountType={account.get('accountType')}, balance={currency} {account.get('balance')}"
            )
        )

    account_context = "\n".join(account_lines)
    full_records_context = json.dumps(accounts, ensure_ascii=False)
    custom_instructions = os.getenv("PURI_BANK_SYSTEM_INSTRUCTIONS", "").strip()

    base_instructions = (
        f"You are a phone banking voice agent for {bank_name}. "
        "Always respond in Hindi unless the customer explicitly asks for English. "
        "Use a friendly, casual, human conversational style while staying clear and helpful. "
        "Your persona is female; use feminine first-person phrasing naturally (for example: 'main madad kar sakti hoon').\n\n"
        "Identity verification policy:\n"
        "1) Verify before sharing account-specific information.\n"
        "2) Ask verification details one by one in separate turns: first accountId, then registered mobile last 4 digits, then date of birth (DD-MM).\n"
        "3) Do not ask multiple verification fields in a single question.\n"
        "4) If verification fails twice, do not disclose account data and offer human-agent escalation.\n\n"
        "Operational policy:\n"
        "- Use ONLY the mock database records provided below.\n"
        "- Do not invent balances, transactions, EMI details, or account numbers.\n"
        "- If record not found, say you cannot find it and ask to re-check details.\n"
        "- For record lookup, match accountId, customerName, mobileLast4, and dobDayMonth exactly.\n"
        "- After successful verification, first confirm verification success and ask how you can help today; do not provide account details until the customer asks.\n"
        "- Share only the details the customer requested (balance, transactions, loan, or all details).\n"
        "- For transactions, include date, description, and amount.\n"
        "- For loan status, include whether loan is active; if active, include EMI amount and next due date; if inactive, clearly say no active loan.\n"
        "- Keep answers concise and call-friendly.\n"
        "- Offer escalation for fraud complaints, repeated verification failures, or user request.\n\n"
        "Post-verification flow (Hindi unless user asks English):\n"
        "1) Say verification is complete.\n"
        "2) Ask: 'Verification complete ho gaya. Aaj main aapki kis baat mein madad kar sakti hoon?'\n"
        "3) Provide only requested details; if customer asks for full account details, then provide balance + last 3 transactions + loan details.\n\n"
        f"Mock database ({bank_name}) quick index:\n{account_context}\n\n"
        f"Mock database ({bank_name}) full records JSON:\n{full_records_context}\n"
    )

    if custom_instructions:
        return f"{base_instructions}\nAdditional instructions:\n{custom_instructions}"
    return base_instructions


def session_config():
    """Returns the default session configuration for Voice Live."""
    voice_name = os.getenv("PURI_BANK_VOICE_NAME", "hi-IN-AnanyaNeural")
    vad_type = os.getenv("AZURE_VOICELIVE_VAD_TYPE", "azure_semantic_vad").strip() or "azure_semantic_vad"
    vad_threshold = _env_float("AZURE_VOICELIVE_VAD_THRESHOLD", 0.3)
    vad_prefix_padding_ms = _env_int("AZURE_VOICELIVE_VAD_PREFIX_PADDING_MS", 200)
    vad_silence_duration_ms = _env_int("AZURE_VOICELIVE_VAD_SILENCE_DURATION_MS", 200)
    vad_speech_duration_ms = _env_int("AZURE_VOICELIVE_VAD_SPEECH_DURATION_MS", 200)
    vad_remove_filler_words = _env_bool("AZURE_VOICELIVE_VAD_REMOVE_FILLER_WORDS", False)
    vad_interrupt_response = _env_bool("AZURE_VOICELIVE_VAD_INTERRUPT_RESPONSE", True)

    end_of_utterance_enabled = _env_bool("AZURE_VOICELIVE_END_OF_UTTERANCE_ENABLED", True)
    end_of_utterance_model = os.getenv(
        "AZURE_VOICELIVE_END_OF_UTTERANCE_MODEL", "semantic_detection_v1"
    ).strip() or "semantic_detection_v1"
    end_of_utterance_timeout_ms = _env_int("AZURE_VOICELIVE_END_OF_UTTERANCE_TIMEOUT_MS", 2000)
    end_of_utterance_timeout_sec = max(0.1, end_of_utterance_timeout_ms / 1000.0)

    noise_reduction_type = os.getenv(
        "AZURE_VOICELIVE_NOISE_REDUCTION_TYPE", "azure_deep_noise_suppression"
    ).strip()
    echo_cancellation_enabled = _env_bool("AZURE_VOICELIVE_ECHO_CANCELLATION_ENABLED", True)

    input_transcription_model = os.getenv(
        "AZURE_VOICELIVE_INPUT_TRANSCRIPTION_MODEL", "whisper-1"
    ).strip()
    if input_transcription_model:
        normalized_model = input_transcription_model.replace("_", "-").lower()
        if normalized_model == "azurespeech":
            normalized_model = "azure-speech"
        input_transcription_model = normalized_model
    input_language = os.getenv("AZURE_VOICELIVE_INPUT_LANGUAGE", "hi-IN").strip()
    if input_language.lower() in {"hi", "hindi"}:
        input_language = "hi-IN"

    instructions = _build_puri_bank_instructions()
    turn_detection = {
        "type": vad_type,
        "threshold": vad_threshold,
        "prefix_padding_ms": vad_prefix_padding_ms,
        "silence_duration_ms": vad_silence_duration_ms,
        "speech_duration_ms": vad_speech_duration_ms,
        "remove_filler_words": vad_remove_filler_words,
        "interrupt_response": vad_interrupt_response,
    }

    if end_of_utterance_enabled:
        turn_detection["end_of_utterance_detection"] = {
            "model": end_of_utterance_model,
            "timeout": end_of_utterance_timeout_sec,
        }

    session = {
        "instructions": instructions,
        "turn_detection": turn_detection,
        "voice": {
            "name": voice_name,
            "type": "azure-standard",
            "temperature": 0.8,
        },
    }

    if noise_reduction_type and noise_reduction_type.lower() != "none":
        session["input_audio_noise_reduction"] = {"type": noise_reduction_type}

    if echo_cancellation_enabled:
        session["input_audio_echo_cancellation"] = {"type": "server_echo_cancellation"}

    if input_transcription_model:
        transcription = {}
        transcription["model"] = input_transcription_model
        if input_language:
            transcription["language"] = input_language
        session["input_audio_transcription"] = transcription
        logger.info(
            "Configured input_audio_transcription model=%s language=%s",
            input_transcription_model,
            input_language or "<none>",
        )

    return {
        "type": "session.update",
        "session": session,
    }


class ACSMediaHandler:
    """Manages audio streaming between client and Azure Voice Live API."""

    def __init__(self, config):
        self.endpoint = config["AZURE_VOICE_LIVE_ENDPOINT"]
        self.model = config["VOICE_LIVE_MODEL"]
        self.byom_profile = config.get("VOICELIVE_BYOM_MODE", "").strip()
        self.foundry_resource_override = config.get("VOICELIVE_FOUNDRY_RESOURCE", "").strip()
        self.api_key = config["AZURE_VOICE_LIVE_API_KEY"]
        self.client_id = config["AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID"]
        self.send_queue = asyncio.Queue()
        self.ws = None
        self.send_task = None
        self.incoming_websocket = None
        self.is_raw_audio = True
        self._greeting_sent = False
        self._session_ready = False
        self._stream_ready = False
        self._greeting_lock = asyncio.Lock()
        self._greeting_delay_seconds = max(0.0, _env_float("AZURE_VOICELIVE_GREETING_DELAY", 0.5))

        # TTS output buffering for continuous ambient mixing
        self._tts_output_buffer = bytearray()
        self._tts_buffer_lock = asyncio.Lock()
        self._max_buffer_size = _env_int("AZURE_VOICELIVE_TTS_MAX_BUFFER_BYTES", 480000)
        self._buffer_warning_logged = False
        self._tts_playback_started = False  # Track if we've started playing TTS
        tts_min_buffer_ms = max(20, _env_int("AZURE_VOICELIVE_TTS_MIN_BUFFER_MS", 100))
        audio_sample_rate = max(8000, _env_int("AZURE_VOICELIVE_AUDIO_SAMPLE_RATE", 24000))
        self._min_buffer_to_start = int((audio_sample_rate * 2 * tts_min_buffer_ms) / 1000)
        
        # Ambient mixer initialization
        self._ambient_mixer: Optional[AmbientMixer] = None
        ambient_preset = config.get("AMBIENT_PRESET", "none")
        if ambient_preset and ambient_preset != "none":
            try:
                self._ambient_mixer = AmbientMixer(preset=ambient_preset)
            except Exception as e:
                logger.error(f"Failed to initialize AmbientMixer: {e}")

    def _generate_guid(self):
        return str(uuid.uuid4())

    async def connect(self):
        """Connects to Azure Voice Live API via WebSocket."""
        endpoint = self.endpoint.rstrip("/")
        model = self.model.strip()

        query = {
            "api-version": "2025-05-01-preview",
            "model": model,
        }
        if self.byom_profile:
            query["profile"] = self.byom_profile
        if self.foundry_resource_override:
            query["foundry-resource-override"] = self.foundry_resource_override

        url = f"{endpoint}/voice-live/realtime?{urlencode(query)}"
        url = url.replace("https://", "wss://")

        self.ws = None

        if not self.client_id:
            raise RuntimeError(
                "AZURE_USER_ASSIGNED_IDENTITY_CLIENT_ID is required for Entra-authenticated Voice Live connection"
            )

        max_attempts = max(1, _env_int("AZURE_VOICELIVE_MI_CONNECT_RETRIES", 3))
        retry_base_delay = max(0.2, _env_float("AZURE_VOICELIVE_MI_RETRY_DELAY_SECONDS", 1.0))
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                async with ManagedIdentityCredential(client_id=self.client_id) as credential:
                    token = await credential.get_token(
                        "https://cognitiveservices.azure.com/.default"
                    )
                    mi_headers = {
                        "x-ms-client-request-id": self._generate_guid(),
                        "Authorization": f"Bearer {token.token}",
                    }
                    self.ws = await ws_connect(url, additional_headers=mi_headers)
                    logger.info(
                        "[VoiceLiveACSHandler] Connected to Voice Live API by managed identity (attempt %s/%s)",
                        attempt,
                        max_attempts,
                    )
                    break
            except Exception as error:
                last_error = error
                logger.warning(
                    "[VoiceLiveACSHandler] Managed identity websocket auth failed (attempt %s/%s): %s",
                    attempt,
                    max_attempts,
                    error,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(retry_base_delay * attempt)

        if self.ws is None:
            raise RuntimeError(
                "Unable to authenticate to Voice Live via managed identity after retries. "
                "Ensure this identity has 'Cognitive Services User' on the Voice Live account and "
                "'Cognitive Services OpenAI User' on the BYOM/OpenAI account."
            ) from last_error

        logger.info(
            "[VoiceLiveACSHandler] Connected to Voice Live API (profile=%s, foundry-resource-override=%s)",
            self.byom_profile or "<none>",
            self.foundry_resource_override or "<none>",
        )

        await self._send_json(session_config())

        asyncio.create_task(self._receiver_loop())
        self.send_task = asyncio.create_task(self._sender_loop())
        await self._try_send_initial_greeting("post-session.update")

    async def _try_send_initial_greeting(self, trigger: str):
        """Sends the initial greeting exactly once when session and stream are ready."""
        async with self._greeting_lock:
            if self._greeting_sent:
                return

            if not self._session_ready or not self._stream_ready:
                logger.info(
                    "[VoiceLiveACSHandler] Greeting deferred by %s (session_ready=%s, stream_ready=%s)",
                    trigger,
                    self._session_ready,
                    self._stream_ready,
                )
                return

            if self._greeting_delay_seconds > 0:
                await asyncio.sleep(self._greeting_delay_seconds)
            await self._send_json({"type": "response.create"})
            self._greeting_sent = True
            logger.info("[VoiceLiveACSHandler] Initial greeting triggered by %s", trigger)

    async def init_incoming_websocket(self, socket, is_raw_audio=True):
        """Sets up incoming ACS WebSocket."""
        self.incoming_websocket = socket
        self.is_raw_audio = is_raw_audio
        if not is_raw_audio:
            self._stream_ready = True

    async def audio_to_voicelive(self, audio_b64: str):
        """Queues audio data to be sent to Voice Live API."""
        await self.send_queue.put(
            json.dumps({"type": "input_audio_buffer.append", "audio": audio_b64})
        )

    async def _send_json(self, obj):
        """Sends a JSON object over WebSocket."""
        if self.ws:
            await self.ws.send(json.dumps(obj))

    async def _sender_loop(self):
        """Continuously sends messages from the queue to the Voice Live WebSocket."""
        try:
            while True:
                msg = await self.send_queue.get()
                if self.ws:
                    await self.ws.send(msg)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Sender loop error")

    async def _receiver_loop(self):
        """Handles incoming events from the Voice Live WebSocket."""
        try:
            async for message in self.ws:
                event = json.loads(message)
                event_type = event.get("type")

                match event_type:
                    case "session.created":
                        session_id = event.get("session", {}).get("id")
                        logger.info("[VoiceLiveACSHandler] Session ID: %s", session_id)
                        self._session_ready = True
                        await self._try_send_initial_greeting("session.created")

                    case "session.updated":
                        session_id = event.get("session", {}).get("id")
                        logger.info("[VoiceLiveACSHandler] Session updated: %s", session_id)
                        self._session_ready = True
                        await self._try_send_initial_greeting("session.updated")

                    case "input_audio_buffer.cleared":
                        logger.info("Input Audio Buffer Cleared Message")

                    case "input_audio_buffer.speech_started":
                        logger.info(
                            "Voice activity detection started at %s ms",
                            event.get("audio_start_ms"),
                        )
                        await self.stop_audio()

                    case "input_audio_buffer.speech_stopped":
                        logger.info("Speech stopped")

                    case "conversation.item.input_audio_transcription.completed":
                        transcript = event.get("transcript")
                        logger.info("User: %s", transcript)

                    case "conversation.item.input_audio_transcription.failed":
                        error_msg = event.get("error")
                        logger.warning("Transcription Error: %s", error_msg)

                    case "response.done":
                        response = event.get("response", {})
                        logger.info("Response Done: Id=%s", response.get("id"))
                        if response.get("status_details"):
                            logger.info(
                                "Status Details: %s",
                                json.dumps(response["status_details"], indent=2),
                            )

                    case "response.audio_transcript.done":
                        transcript = event.get("transcript")
                        logger.info("AI: %s", transcript)
                        await self.send_message(
                            json.dumps({"Kind": "Transcription", "Text": transcript})
                        )

                    case "response.audio.delta":
                        delta = event.get("delta")
                        audio_bytes = base64.b64decode(delta)
                        
                        # Check if ambient mixing is enabled
                        if self._ambient_mixer is not None and self._ambient_mixer.is_enabled():
                            # Buffer TTS for continuous output mixing
                            async with self._tts_buffer_lock:
                                self._tts_output_buffer.extend(audio_bytes)
                                # Warn if buffer is getting large, but NEVER drop audio
                                if len(self._tts_output_buffer) > self._max_buffer_size:
                                    if not self._buffer_warning_logged:
                                        logger.warning(
                                            f"TTS buffer large: {len(self._tts_output_buffer)} bytes. "
                                            "Speech may be delayed but will not be cut."
                                        )
                                        self._buffer_warning_logged = True
                                elif self._buffer_warning_logged and len(self._tts_output_buffer) < self._max_buffer_size // 2:
                                    self._buffer_warning_logged = False  # Reset warning flag
                        else:
                            # No ambient - send immediately (original behavior)
                            if self.is_raw_audio:
                                await self.send_message(audio_bytes)
                            else:
                                await self.voicelive_to_acs(delta)

                    case "error":
                        logger.error("Voice Live Error: %s", event)

                    case _:
                        logger.debug(
                            "[VoiceLiveACSHandler] Other event: %s", event_type
                        )
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Receiver loop error")

    async def send_message(self, message: Data):
        """Sends data back to client WebSocket."""
        try:
            await self.incoming_websocket.send(message)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Failed to send message")

    async def voicelive_to_acs(self, base64_data):
        """Converts Voice Live audio delta to ACS audio message."""
        try:
            data = {
                "kind": "AudioData",
                "audioData": {"data": base64_data},
                "stopAudio": None,
            }
            await self.send_message(json.dumps(data))
            logger.debug("[VoiceLiveACSHandler] Sent audio chunk to ACS")
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error in voicelive_to_acs")

    async def stop_audio(self):
        """Sends a StopAudio signal to ACS."""
        stop_audio_data = {"kind": "StopAudio", "audioData": None, "stopAudio": {}}
        await self.send_message(json.dumps(stop_audio_data))
        
        # Clear TTS buffer when user starts speaking
        if self._ambient_mixer is not None:
            async with self._tts_buffer_lock:
                self._tts_output_buffer.clear()
                self._tts_playback_started = False

    async def _send_continuous_audio(self, chunk_size: int) -> None:
        """
        Send continuous audio (ambient + TTS if available) back to client.
        
        Called for every incoming audio frame, ensuring continuous output.
        Uses buffered TTS with minimum buffer threshold to prevent mid-word cuts.
        
        Args:
            chunk_size: Size of audio chunk to send (matches incoming frame size)
        """
        if self._ambient_mixer is None or not self._ambient_mixer.is_enabled():
            return  # Ambient disabled, skip
            
        try:
            async with self._tts_buffer_lock:
                buffer_len = len(self._tts_output_buffer)
                
                # Always get a consistent ambient chunk first
                ambient_bytes = self._ambient_mixer.get_ambient_only_chunk(chunk_size)
                
                # Determine if we should play TTS
                should_play_tts = False
                if self._tts_playback_started:
                    # Already playing - continue until buffer empty
                    if buffer_len >= chunk_size:
                        should_play_tts = True
                    elif buffer_len > 0:
                        # Partial buffer but still playing - use what we have
                        should_play_tts = True
                    else:
                        # Buffer empty - stop playback mode
                        self._tts_playback_started = False
                else:
                    # Not yet playing - wait for minimum buffer
                    if buffer_len >= self._min_buffer_to_start:
                        self._tts_playback_started = True
                        should_play_tts = True
                
                if should_play_tts and buffer_len >= chunk_size:
                    # Full TTS chunk available - add TTS on top of ambient
                    tts_chunk = bytes(self._tts_output_buffer[:chunk_size])
                    del self._tts_output_buffer[:chunk_size]
                    
                    # Mix: ambient (constant) + TTS
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    mixed = ambient + tts
                    mixed = np.clip(mixed, -0.95, 0.95)  # Soft limit
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                elif should_play_tts and buffer_len > 0:
                    # Partial TTS remaining at end of speech - drain it
                    tts_chunk = bytes(self._tts_output_buffer[:])
                    self._tts_output_buffer.clear()
                    self._tts_playback_started = False
                    
                    ambient = np.frombuffer(ambient_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                    
                    # Only mix TTS for the portion we have
                    tts_samples = len(tts_chunk) // 2
                    tts = np.frombuffer(tts_chunk, dtype=np.int16).astype(np.float32) / 32768.0
                    ambient[:tts_samples] += tts
                    mixed = np.clip(ambient, -0.95, 0.95)
                    output_bytes = (mixed * 32767).astype(np.int16).tobytes()
                    
                else:
                    # No TTS ready - just send constant ambient
                    output_bytes = ambient_bytes
            
            # Send to client
            if self.is_raw_audio:
                # Web browser - raw bytes
                await self.send_message(output_bytes)
            else:
                # Phone call - JSON wrapped
                output_b64 = base64.b64encode(output_bytes).decode("ascii")
                data = {
                    "kind": "AudioData",
                    "audioData": {"data": output_b64},
                    "stopAudio": None,
                }
                await self.send_message(json.dumps(data))
                
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error in _send_continuous_audio")

    async def acs_to_voicelive(self, stream_data):
        """Processes audio from ACS and forwards to Voice Live if not silent."""
        try:
            data = json.loads(stream_data)
            kind = data.get("kind") or data.get("Kind")

            if not self._stream_ready and kind in {"AudioData", "AudioMetadata"}:
                self._stream_ready = True
                await self._try_send_initial_greeting("acs.stream.ready")

            if kind == "AudioData":
                audio_data = data.get("audioData") or data.get("AudioData") or {}
                incoming_data = audio_data.get("data") or audio_data.get("Data") or ""
                
                # Determine chunk size from incoming audio
                if incoming_data:
                    incoming_bytes = base64.b64decode(incoming_data)
                    chunk_size = len(incoming_bytes)
                else:
                    chunk_size = DEFAULT_CHUNK_SIZE
                
                # Send continuous audio back to caller (ambient + TTS mixed)
                await self._send_continuous_audio(chunk_size)
                
                # Forward non-silent audio to Voice Live (existing logic)
                is_silent = audio_data.get("silent")
                if is_silent is None:
                    is_silent = audio_data.get("Silent", True)
                if not is_silent:
                    await self.audio_to_voicelive(incoming_data)
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error processing ACS audio")

    async def web_to_voicelive(self, audio_bytes):
        """Encodes raw audio bytes and sends to Voice Live API."""
        if not self._stream_ready:
            self._stream_ready = True
            await self._try_send_initial_greeting("web.stream.ready")

        chunk_size = len(audio_bytes)
        
        # Send continuous audio back to browser (ambient + TTS mixed)
        await self._send_continuous_audio(chunk_size)
        
        # Forward to Voice Live
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        await self.audio_to_voicelive(audio_b64)

    async def stop_audio_output(self):
        """Gracefully stops websocket tasks and closes Voice Live connection."""
        try:
            if self.send_task and not self.send_task.done():
                self.send_task.cancel()
                try:
                    await self.send_task
                except asyncio.CancelledError:
                    pass
            if self.ws:
                await self.ws.close()
                self.ws = None
        except Exception:
            logger.exception("[VoiceLiveACSHandler] Error during stop_audio_output")
