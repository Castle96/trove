"""Synchronous voice worker ("Jarvis") that reports turns to dockwatch.

Captures a spoken request and runs the pipeline the Voice tab visualizes:

    VAD -> STT -> NLU -> agent -> skills -> TTS

Each stage records its own latency; the completed turn (transcript, reply,
status, per-stage timings) is pushed to ``POST /api/voice/ingest`` — the
contract in ``app/schemas/voice.py`` and ``app/api/voice.py``. The Jarvis
swarm agent heartbeats automatically on the server side.

Audio input modes
-----------------
* ``--file path`` — one-off audio file
* ``--dir dir`` — watch folder; new files are transcribed then moved to ``dir/done``
* ``--mic`` — live microphone (requires system portaudio + the ``sounddevice``
  package, which is *not* installed by default)

The STT and TTS stages use real models (faster-whisper, piper-tts). The
``--fake-stt`` / ``--fake-tts`` flags swap in synthetic stages so the full
pipeline and the ingest contract can be exercised with zero model downloads.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.worker")

# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass
class WorkerConfig:
    """Worker settings (CLI flags override the env defaults)."""

    url: str
    token: str
    session_id: str
    whisper_model: str
    language: str
    tts_voice: str
    voices_dir: Path
    out_dir: Path
    play: bool
    llm_url: str | None
    fake_stt: bool
    fake_tts: bool
    fake_transcript: str
    input_file: str | None = None
    input_dir: str | None = None
    mic: bool = False
    poll: float = 2.0
    timeout: float = 10.0
    skills: list[str] = field(default_factory=lambda: ["containers", "health"])

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


@dataclass
class Stage:
    """One pipeline stage measurement pushed to the server."""

    stage: str
    latency_ms: int
    status: str = "ok"
    detail: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "detail": self.detail,
        }


class _Timer:
    """Accumulate wall time and report milliseconds for one stage."""

    def __init__(self, stage: str, stages: list[Stage], status: str = "ok") -> None:
        self.stage = stage
        self.stages = stages
        self.status = status
        self.detail: str | None = None
        self._t0 = time.perf_counter()

    def __enter__(self) -> _Timer:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        latency = int((time.perf_counter() - self._t0) * 1000)
        status = "error" if exc is not None else self.status
        if exc is not None:
            self.detail = str(exc)
        self.stages.append(Stage(self.stage, latency, status, self.detail))
        if exc is not None:
            logger.error("stage %s failed: %s", self.stage, exc)
        # Always suppress: run_turn inspects stages[-1].status to decide
        # whether to short-circuit and ingest an error turn.
        return True


# ---------------------------------------------------------------------------
# audio io + VAD (stage 1)
# ---------------------------------------------------------------------------

_REQ_SUFFIXES = ("*.wav", "*.mp3", "*.ogg", "*.flac", "*.m4a")


def decode_audio(path: str | Path) -> Any:
    """Decode an audio file to mono float32 PCM at 16 kHz (numpy array)."""
    import av  # PyAV (already a faster-whisper dependency)

    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16000)
        frames: list[Any] = []
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                frames.append(out.to_ndarray())
        for out in resampler.resample(None):
            frames.append(out.to_ndarray())
    if not frames:
        raise ValueError(f"no audio frames in {path}")
    import numpy as np

    return np.concatenate(frames, axis=1).squeeze().astype(np.float32)


def detect_speech(audio: Any, min_duration_s: float = 0.2) -> tuple[bool, float, float]:
    """Crude energy VAD: ``(has_speech, rms, peak_abs)`` over a 16 kHz mono clip."""
    import numpy as np

    x = np.asarray(audio, dtype=np.float32)
    if x.ndim != 1 or x.size == 0:
        return False, 0.0, 0.0
    rms = float(np.sqrt(np.mean(np.square(x))))
    peak = float(np.max(np.abs(x)))
    voiced_s = float(np.count_nonzero(np.abs(x) > 0.02)) / 16000.0
    return rms > 1e-3 and peak > 2e-3 and voiced_s >= min_duration_s, rms, peak


def _silence_wav(path: Path, seconds: float = 1.0, rate: int = 16000) -> None:
    """Write a valid silent mono 16-bit WAV (used by ``--fake-tts``)."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))


# ---------------------------------------------------------------------------
# STT (stage 2)
# ---------------------------------------------------------------------------


def _load_whisper(model_size: str) -> Callable[[Any, str], tuple[str, str, float]]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def transcribe(audio: Any, language: str) -> tuple[str, str, float]:
        segments, info = model.transcribe(audio, language=language, beam_size=5)
        text = "".join(segment.text for segment in segments).strip()
        return text, str(info.language), float(info.language_probability)

    return transcribe


# ---------------------------------------------------------------------------
# NLU (stage 3)
# ---------------------------------------------------------------------------

_INTENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greeting", re.compile(r"^(hi|hello|hey|good (morning|afternoon|evening))\b", re.I)),
    ("health", re.compile(r"\b(health|healthy|alive|are you (ok|up|there)|status)\b", re.I)),
    ("containers", re.compile(r"\b(container|stack|service)s?\b", re.I)),
    ("who", re.compile(r"\bwho are you\b", re.I)),
    ("time", re.compile(r"\b(time|clock|date)\b", re.I)),
    ("help", re.compile(r"\b(help|what can you do|capabilities)\b", re.I)),
)


def classify_intent(transcript: str) -> tuple[str, str | None]:
    """Rule-based intent classification (returns ``(intent, detail)``)."""
    for intent, pattern in _INTENTS:
        if pattern.search(transcript):
            return intent, pattern.pattern
    return "chat", None


# ---------------------------------------------------------------------------
# skills (stage 5) — real dockwatch API calls
# ---------------------------------------------------------------------------


class DockwatchSkills:
    """Small dockwatch skill set executed during the ``skills`` stage."""

    def __init__(self, cfg: WorkerConfig) -> None:
        self.cfg = cfg

    def _get(self, path: str) -> Any:
        import httpx

        resp = httpx.get(
            f"{self.cfg.url}{path}",
            headers=self.cfg.auth_headers,
            timeout=self.cfg.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def health(self) -> str:
        body = self._get("/api/health")
        if body.get("status") == "ok":
            return "Dockwatch is healthy; the API responds."
        return "Dockwatch reports an unhealthy status."

    def containers(self) -> str:
        items = self._get("/api/docker/containers")
        total = len(items)
        running = sum(1 for c in items if c.get("state") == "running")
        if total == 0:
            return "No containers are currently visible."
        return f"{running} of {total} containers are running."

    def dispatch(self, intent: str) -> tuple[str, str, str | None]:
        """Run the skill for an intent: ``(status, reply_fragment, detail)``."""
        if intent == "containers":
            return "ok", self.containers(), None
        if intent == "health":
            return "ok", self.health(), None
        return "ok", "", "no skill to run"


# ---------------------------------------------------------------------------
# agent (stage 4) — decide and reply
# ---------------------------------------------------------------------------

_GREETING = "Hello! I am Jarvis, your dockwatch voice assistant."
_WHO = "I am Jarvis, a voice assistant wired into your dockwatch deployment."
_HELP = "I can check dockwatch health, count running containers, tell the time, or just chat."


def _reply_for(
    cfg: WorkerConfig,
    intent: str,
    transcript: str,
    skill_result: str,
) -> str:
    """Assemble the agent's reply for the classified intent."""
    if intent == "greeting":
        return _GREETING
    if intent == "who":
        return _WHO
    if intent == "help":
        return _HELP
    if intent == "time":
        return time.strftime("It is %H:%M on %A, %B %d.")
    if intent in {"containers", "health"}:
        return skill_result or "I could not reach the dockwatch API for that."
    if cfg.llm_url and intent in {"chat", "health", "containers"}:
        try:
            return _llm_chat(cfg, transcript)
        except Exception as exc:  # never let the LLM take the turn down
            logger.warning("LLM unavailable (%s); falling back to canned reply", exc)
            return f"I heard: {transcript}"
    return f"I heard: {transcript}"


def _llm_chat(cfg: WorkerConfig, transcript: str) -> str:
    import httpx

    resp = httpx.post(
        f"{cfg.llm_url}/api/generate",
        json={
            "model": "deepseek-r1:1.5b",
            "prompt": f"You are Jarvis, a terse dockwatch voice assistant. "
            f'Reply in <=2 sentences. User said: "{transcript}"',
            "stream": False,
        },
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    return str(resp.json().get("response", "")).strip() or f"I heard: {transcript}"


# ---------------------------------------------------------------------------
# TTS (stage 6)
# ---------------------------------------------------------------------------


def _ensure_piper(voice: str, voices_dir: Path) -> Any:
    from piper import PiperVoice
    from piper.download_voices import download_voice

    voices_dir.mkdir(parents=True, exist_ok=True)
    model_file = voices_dir / f"{voice}.onnx"
    config_file = voices_dir / f"{voice}.onnx.json"
    if not model_file.exists() or not config_file.exists():
        logger.info("downloading piper voice %s -> %s", voice, voices_dir)
        download_voice(voice, voices_dir)
    # load() infers <model>.json by default; the downloader stores
    # <voice>.onnx.json, so point at it explicitly.
    return PiperVoice.load(model_file, config_path=config_file)


def _synthesize_piper(voice: Any, text: str, out_path: Path) -> None:
    with wave.open(str(out_path), "wb") as wf:
        voice.synthesize_wav(text, wf)


def _play_audio(path: Path) -> None:
    for player in ("paplay", "aplay"):
        if shutil_which(player):
            subprocess.run([player, str(path)], check=False)
            return
    logger.warning("no audio player found (paplay/aplay); wrote %s", path)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


# ---------------------------------------------------------------------------
# turn orchestration
# ---------------------------------------------------------------------------


def run_turn(
    audio_path: Path, cfg: WorkerConfig, whisper: Callable[..., Any] | None, piper: Any
) -> dict[str, Any]:
    """Execute one full turn against an audio file and ingest the telemetry.

    A VAD/STT failure short-circuits: downstream stages are skipped and the
    error turn (with the stages recorded so far) is ingested so dockwatch can
    flag it. A skills failure is non-fatal (the reply simply degrades).
    """
    stages: list[Stage] = []
    transcript = ""

    # VAD -> decode + speech detection
    with _Timer("vad", stages) as vad:
        audio = decode_audio(audio_path)
        has_speech, rms, peak = detect_speech(audio)
        vad.detail = f"rms={rms:.3f} peak={peak:.3f}"
        if not has_speech:
            raise ValueError("no speech detected")
    if stages[-1].status == "error":
        return _finalize(cfg, audio_path, stages, transcript, "", "error", "vad")

    # STT
    with _Timer("stt", stages) as stt:
        if cfg.fake_stt or whisper is None:
            transcript = cfg.fake_transcript
            stt.detail = "synthetic transcript (--fake-stt)"
        else:
            transcript, lang, prob = whisper(audio, cfg.language)
            stt.detail = f"lang={lang} p={prob:.2f}"
            if not transcript:
                raise ValueError("speech detected but transcript empty — likely noise")
    if stages[-1].status == "error":
        return _finalize(cfg, audio_path, stages, transcript, "", "error", "stt")

    # NLU (regex classification; cannot realistically fail)
    with _Timer("nlu", stages) as nlu:
        intent, detail = classify_intent(transcript)
        nlu.detail = detail or intent

    # skills (real dockwatch API calls; a failed skill degrades the reply but
    # the turn still completes)
    skill_result = ""
    with _Timer("skills", stages) as skills:
        try:
            skill_status, skill_result, skill_detail = DockwatchSkills(cfg).dispatch(intent)
            skills.status = skill_status
            skills.detail = skill_detail or (skill_result[:80] if skill_result else "no skill")
        except Exception as exc:
            skills.status = "error"
            skills.detail = f"skill failed: {exc}"
            skill_result = ""

    # agent -> assemble the reply
    with _Timer("agent", stages) as agent:
        response_text = _reply_for(cfg, intent, transcript, skill_result)
        agent.detail = intent

    # TTS
    out_file = cfg.out_dir / f"{audio_path.stem}.reply.wav"
    with _Timer("tts", stages) as tts:
        if cfg.fake_tts or piper is None:
            _silence_wav(out_file)
            tts.detail = "synthetic wav (--fake-tts)"
        else:
            _synthesize_piper(piper, response_text, out_file)
        if cfg.play:
            _play_audio(out_file)
    if stages[-1].status == "error":
        return _finalize(cfg, audio_path, stages, transcript, response_text, "error", "tts")

    return _finalize(cfg, audio_path, stages, transcript, response_text, "ok", intent)


def _finalize(
    cfg: WorkerConfig,
    audio_path: Path,
    stages: list[Stage],
    transcript: str,
    response_text: str,
    status: str,
    intent: str,
) -> dict[str, Any]:
    """Ingest a completed/failed turn and return the turn summary."""
    payload = {
        "session_id": cfg.session_id,
        "transcript": transcript.strip() or None,
        "response_text": response_text or None,
        "status": status,
        "stages": [s.payload() for s in stages],
    }
    ingested = _ingest(cfg, payload)
    logger.info(
        "turn %s | intent=%s | status=%s | total=%dms | ingested=%s | reply: %s",
        audio_path.name,
        intent,
        status,
        sum(s.latency_ms for s in stages),
        ingested,
        response_text[:80] if response_text else "-",
    )
    return {"transcript": transcript, "intent": intent, "response": response_text, "stages": stages}


def _ingest(cfg: WorkerConfig, payload: dict[str, Any]) -> int:
    import httpx

    resp = httpx.post(
        f"{cfg.url}/api/voice/ingest",
        json=payload,
        headers=cfg.auth_headers,
        timeout=cfg.timeout,
    )
    resp.raise_for_status()
    return resp.status_code


# ---------------------------------------------------------------------------
# in-flight heartbeat (Jarvis "working" while the turn is processed)
# ---------------------------------------------------------------------------


def _find_jarvis_agent_id(cfg: WorkerConfig) -> int | None:
    """Best-effort lookup of the jarvis swarm agent id (None when unavailable)."""
    import httpx

    try:
        resp = httpx.get(f"{cfg.url}/api/swarm", headers=cfg.auth_headers, timeout=cfg.timeout)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("cannot list swarm agents for heartbeat: %s", exc)
        return None
    for agent in resp.json():
        if agent.get("name") == "jarvis":
            raw_id = agent.get("id")
            return raw_id if isinstance(raw_id, int) else None
    logger.warning("jarvis agent not present on %s; in-flight heartbeat skipped", cfg.url)
    return None


def _best_effort_heartbeat(
    cfg: WorkerConfig, agent_id: int, status: str, snippet: str | None = None
) -> None:
    """Report in-flight state to the swarm dashboard; never fails the turn."""
    import httpx

    try:
        httpx.post(
            f"{cfg.url}/api/swarm/agents/{agent_id}/heartbeat",
            json={"status": status, "conversation_snippet": snippet},
            headers=cfg.auth_headers,
            timeout=cfg.timeout,
        )
    except Exception as exc:
        logger.warning("jarvis heartbeat failed: %s", exc)


def _process(
    audio_path: Path, cfg: WorkerConfig, whisper: Callable[..., Any] | None, piper: Any
) -> dict[str, Any]:
    """Heartbeat ``working``, run the turn, ingest (server settles Jarvis to idle)."""
    agent_id = None if cfg.fake_stt and cfg.fake_tts else _find_jarvis_agent_id(cfg)
    if agent_id is not None:
        _best_effort_heartbeat(cfg, agent_id, "working", snippet=audio_path.name)
    return run_turn(audio_path, cfg, whisper, piper)


def _load_models(cfg: WorkerConfig) -> tuple[Callable[..., Any] | None, Any]:
    whisper = None if cfg.fake_stt else _load_whisper(cfg.whisper_model)
    piper = None if cfg.fake_tts else _ensure_piper(cfg.tts_voice, cfg.voices_dir)
    return whisper, piper


def _run_file(path: Path, cfg: WorkerConfig) -> int:
    whisper, piper = _load_models(cfg)
    _process(path, cfg, whisper, piper)
    return 0


def _run_dir(cfg: WorkerConfig) -> int:
    folder = Path(cfg.input_dir or ".")
    folder.mkdir(parents=True, exist_ok=True)
    done = folder / "done"
    done.mkdir(exist_ok=True)
    whisper, piper = _load_models(cfg)
    logger.info("watching %s (poll %.1fs)", folder, cfg.poll)
    while True:
        for pattern in _REQ_SUFFIXES:
            for path in sorted(folder.glob(pattern)):
                if path.stat().st_size == 0:
                    continue
                try:
                    _process(path, cfg, whisper, piper)
                    path.rename(done / path.name)
                except Exception as exc:
                    logger.error("turn failed for %s: %s", path.name, exc)
        time.sleep(cfg.poll)


def _run_mic(cfg: WorkerConfig) -> int:
    try:
        import sounddevice as sd
    except ImportError:
        logger.error(
            "mic mode needs the optional extra + system portaudio: "
            "`uv sync --extra voice-mic` and `sudo dnf install portaudio`"
        )
        return 2
    whisper, piper = _load_models(cfg)
    logger.info("listening on the default microphone (Ctrl-C to stop)")
    with sd.InputStream(samplerate=16000, channels=1, dtype="float32") as stream:
        buffer: list[Any] = []
        while True:
            block, _ = stream.read(16000)
            buffer.append(block[:, 0])
            if len(buffer) < 8:  # ~1 s of audio
                continue
            import numpy as np

            audio = np.concatenate(buffer)
            buffer = []
            has_speech, _, _ = detect_speech(audio)
            if not has_speech:
                continue
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                _write_float32_wav(tmp.name, audio)
                _process(Path(tmp.name), cfg, whisper, piper)
    return 0


def _write_float32_wav(path: str, audio: Any) -> None:
    import numpy as np

    pcm = (np.clip(np.asarray(audio), -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis-worker",
        description=(
            "Jarvis voice worker: VAD/STT/NLU/agent/skills/TTS stages report "
            "turns to dockwatch /api/voice/ingest"
        ),
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="dockwatch base URL")
    parser.add_argument(
        "--token",
        default=os.environ.get("DOCKWATCH_AUTH_TOKEN", ""),
        help="DOCKWATCH_AUTH_TOKEN when the API is token-protected",
    )
    parser.add_argument("--session-id", default=f"jarvis-{_hostname()}", help="worker session id")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="process one audio file and exit")
    src.add_argument("--dir", help="watch a directory for new audio files")
    src.add_argument("--mic", action="store_true", help="live microphone mode")
    parser.add_argument("--whisper-model", default="base.en", help="faster-whisper model size")
    parser.add_argument("--language", default="en", help="STT language code")
    parser.add_argument("--tts-voice", default="en_US-lessac-medium", help="piper voice name")
    parser.add_argument(
        "--voices-dir",
        default=str(Path.home() / ".local" / "share" / "piper-voices"),
        help="directory for piper voice models",
    )
    parser.add_argument("--out-dir", default="jarvis-audio", help="TTS output directory")
    parser.add_argument("--play", action="store_true", help="play the reply via paplay/aplay")
    parser.add_argument("--llm-url", default="", help="optional Ollama URL for open-ended replies")
    parser.add_argument("--fake-stt", action="store_true", help="skip whisper (offline smoke run)")
    parser.add_argument("--fake-tts", action="store_true", help="skip piper (offline smoke run)")
    parser.add_argument(
        "--fake-transcript", default="How many containers are running?", help="text for --fake-stt"
    )
    parser.add_argument("--poll", type=float, default=2.0, help="watch-dir poll interval (s)")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _hostname() -> str:
    import socket

    return socket.gethostname() or "host"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = WorkerConfig(
        url=args.url.rstrip("/"),
        token=args.token,
        session_id=args.session_id,
        whisper_model=args.whisper_model,
        language=args.language,
        tts_voice=args.tts_voice,
        voices_dir=Path(args.voices_dir),
        out_dir=Path(args.out_dir),
        play=args.play,
        llm_url=args.llm_url.rstrip("/") if args.llm_url else None,
        fake_stt=args.fake_stt,
        fake_tts=args.fake_tts,
        fake_transcript=args.fake_transcript,
        input_file=args.file,
        input_dir=args.dir,
        mic=args.mic,
        poll=args.poll,
    )
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("jarvis worker session=%s -> %s", cfg.session_id, cfg.url)

    if cfg.input_file:
        return _run_file(Path(cfg.input_file), cfg)
    if cfg.input_dir:
        return _run_dir(cfg)
    return _run_mic(cfg)


if __name__ == "__main__":
    sys.exit(main())
