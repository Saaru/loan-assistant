"""Speech in and out for the voice channel.

Speech-to-text and text-to-speech go through any OpenAI-compatible audio API
(Groq's Whisper and Orpheus by default). The conversation itself runs through the
same orchestrator, tools and actions as chat; only the reply style changes.

`speakable()` turns a reply written for the screen into something a voice can say:
amounts in lakhs and thousands, full dates, no markdown, no rule IDs read aloud.
"""

import asyncio
import io
import os
import re
import threading
import wave
from collections import OrderedDict
from pathlib import Path
from decimal import ROUND_HALF_UP, Decimal

import openai
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[2]

STT_PROMPT = ("Personal loan and credit card servicing in India. EMI, prepayment, foreclosure, NACH mandate, "
              "bounce charge, tenure, lakh, rupees, OTP, Northwind.")

MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June",
          "Jul": "July", "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}


def _rupees_in_words(value: Decimal) -> str:
    if value >= 1000:
        value = value.quantize(Decimal(1), rounding=ROUND_HALF_UP)  # nobody says paise on a large amount
    whole = int(value)
    paise = int((value - whole) * 100)
    parts = []
    crore, rest = divmod(whole, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1000)
    if crore:
        parts.append(f"{crore} crore")
    if lakh:
        parts.append(f"{lakh} lakh")
    if thousand:
        parts.append(f"{thousand} thousand")
    if rest or not parts:
        parts.append(str(rest))
    text = " ".join(parts) + (" rupee" if whole == 1 and not parts[:-1] else " rupees")
    if paise:
        text += f" {paise} paise"
    return text


def speakable(text: str) -> str:
    text = text.replace("\u202f", " ").replace("\u00a0", " ").replace("\u2011", "-")   # model's typographic spaces
    text = re.sub(r"\[(?:[A-Z]{2,4}-)+[A-Z0-9]+\]", "", text)                     # rule citations
    text = re.sub(r"[*_`#>]+", "", text)                                           # markdown emphasis/headings
    text = re.sub(r"^[ \t]*[-•][ \t]+", "", text, flags=re.M)                      # bullets
    text = re.sub(r"^[ \t]*\|?[-:| \t]+\|?[ \t]*$", "", text, flags=re.M)          # table rules
    text = re.sub(r"^[ \t]*\|[ \t]*|[ \t]*\|[ \t]*$", "", text, flags=re.M)        # table edges
    text = text.replace("|", ", ")
    text = re.sub(r"₹\s?([\d,]+(?:\.\d+)?)", lambda m: _rupees_in_words(Decimal(m.group(1).replace(",", ""))), text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s?%", lambda m: f"{Decimal(m.group(1)).normalize():f} percent", text)
    text = re.sub(r"\b0?(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w* (\d{4})\b",
                  lambda m: f"{int(m.group(1))} {MONTHS[m.group(2)]} {m.group(3)}", text)
    text = text.replace("p.a.", "a year").replace("&", " and ").replace("→", " to ").replace("–", " to ")
    text = re.sub(r"[ \t]*\n+[ \t]*", ". ", text)                                  # line breaks end a sentence
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.!?])", r"\1", text)
    text = re.sub(r"[,.](?:[ \t]*[,.])+", lambda m: "." if "." in m.group(0) else ",", text)  # ", ." runs
    text = re.sub(r"([!?])\.", r"\1", text).strip(" ,")
    if text and text[-1] not in ".!?":
        text += "."
    return text


# Phrases Whisper tends to produce from silence or noise.
HALLUCINATIONS = {"thank you", "thank you very much", "thanks for watching", "thanks", "you", "bye", "okay", "hmm",
                  "uh", "um", "so", "the end"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


PROMPT_WORDS = set(re.findall(r"[a-z0-9]+", STT_PROMPT.lower()))


def is_noise(transcript: str, no_speech_prob: float, avg_logprob: float = 0.0) -> bool:
    """Silence, noise, or text Whisper made up: nothing the customer actually said."""
    words = _words(transcript)
    if not words or " ".join(words) in HALLUCINATIONS:
        return True
    if no_speech_prob > 0.6 or (no_speech_prob > 0.35 and avg_logprob < -0.8):
        return True
    # On near-silence Whisper tends to repeat its vocabulary hint back ("EMI, prepayment, foreclosure, NACH.").
    return len(words) >= 2 and sum(w in PROMPT_WORDS for w in words) / len(words) >= 0.8


def is_echo(transcript: str, heard: str) -> bool:
    """True when the 'customer' is really the assistant's own voice picked up by the microphone."""
    words, spoken = _words(transcript), set(_words(heard))
    if len(words) < 2 or not spoken:
        return False
    return sum(w in spoken for w in words) / len(words) >= 0.7


class SpeechUnavailable(Exception):
    pass


class VoiceService:
    """Speech-to-text through an OpenAI-compatible API (Groq Whisper). Text-to-speech through one engine,
    chosen at start-up so a conversation never switches voices:

      piper  local open-source model, no API, no limits (default when the voice model is present)
      groq   Groq's Orpheus voice (its free tier allows only a few minutes of speech a day)
      none   no server voice; the browser speaks instead
    """

    def __init__(self):
        key = os.getenv("LA_SPEECH_API_KEY") or os.getenv("GROQ_API_KEY", "")
        base_url = os.getenv("LA_SPEECH_BASE_URL", "https://api.groq.com/openai/v1")
        self.stt_model = os.getenv("LA_STT_MODEL", "whisper-large-v3-turbo")
        self.client = AsyncOpenAI(base_url=base_url, api_key=key, max_retries=1) if key else None
        self.piper_path = Path(os.getenv("LA_PIPER_VOICE", ROOT / "data" / "voices" / "en_US-lessac-medium.onnx"))
        self.tts_model = os.getenv("LA_TTS_MODEL", "canopylabs/orpheus-v1-english")
        self.tts_voice = os.getenv("LA_TTS_VOICE", "autumn")
        requested = os.getenv("LA_TTS_ENGINE", "auto").lower()
        if requested == "auto":
            requested = "piper" if self.piper_path.exists() else ("groq" if key else "none")
        self.tts_engine = requested
        self.tts_disabled_reason: str | None = None
        self._piper = None
        self._piper_lock = threading.Lock()
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        if self.tts_engine == "piper":
            try:
                from piper import PiperVoice
                self._piper = PiperVoice.load(str(self.piper_path))
                self._synthesize_piper("Ready.")  # warm up so the first real sentence is fast
            except Exception as exc:  # missing package or model: fall back to the browser's voice
                self.tts_engine, self.tts_disabled_reason = "none", f"Piper unavailable: {exc}"
        elif self.tts_engine == "groq" and not key:
            self.tts_engine, self.tts_disabled_reason = "none", "no speech API key"
        elif self.tts_engine == "none":
            self.tts_disabled_reason = "server voice turned off"

    @property
    def stt_available(self) -> bool:
        return self.client is not None

    @property
    def tts_available(self) -> bool:
        return self.tts_engine != "none" and self.tts_disabled_reason is None

    async def transcribe(self, audio: bytes, filename: str = "speech.wav") -> tuple[str, float, float]:
        """Returns the transcript, how likely the audio held no speech (0 to 1), and Whisper's average log
        probability (very low values mean it was guessing)."""
        if self.client is None:
            raise SpeechUnavailable("Speech-to-text is not configured (set GROQ_API_KEY).")
        result = await self.client.audio.transcriptions.create(
            model=self.stt_model, file=(filename, audio, "audio/wav"), language="en", prompt=STT_PROMPT,
            response_format="verbose_json", temperature=0)
        segments = getattr(result, "segments", None) or []

        def values(key):
            found = [(seg.get(key) if isinstance(seg, dict) else getattr(seg, key, None)) for seg in segments]
            return [v for v in found if v is not None]

        no_speech, logprob = values("no_speech_prob"), values("avg_logprob")
        return ((result.text or "").strip(), min(no_speech) if no_speech else 0.0,
                max(logprob) if logprob else 0.0)

    async def synthesize(self, text: str) -> bytes:
        if not self.tts_available:
            raise SpeechUnavailable(self.tts_disabled_reason or "Text-to-speech unavailable")
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        if self.tts_engine == "piper":
            audio = await asyncio.to_thread(self._synthesize_piper, text)  # CPU work off the event loop
        else:
            audio = await self._synthesize_groq(text)
        self._cache[text] = audio
        if len(self._cache) > 300:
            self._cache.popitem(last=False)
        return audio

    def _synthesize_piper(self, text: str) -> bytes:
        buffer = io.BytesIO()
        with self._piper_lock, wave.open(buffer, "wb") as wav:
            self._piper.synthesize_wav(text, wav)
        return buffer.getvalue()

    async def _synthesize_groq(self, text: str) -> bytes:
        try:
            response = await self.client.audio.speech.create(model=self.tts_model, voice=self.tts_voice, input=text,
                                                             response_format="wav")
        except (openai.RateLimitError, openai.APIConnectionError, openai.APIStatusError) as exc:
            raise SpeechUnavailable(str(exc))
        return response.read() if hasattr(response, "read") else response.content
