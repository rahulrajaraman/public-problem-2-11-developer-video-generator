"""Read the storyboard narration aloud and align it to each beat.

Speech starts with the frame. Silence fills the rest of the beat so the
retrieved evidence stays on screen after the line is spoken.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
import subprocess

from devvideo.script import Storyboard


DEFAULT_VOICE = "en-US-GuyNeural"
LEAD_IN_MS = 180


class TtsError(RuntimeError):
    pass


def narrate(board: Storyboard, out_dir: Path, voice: str | None = None, max_seconds: float = 58.0) -> Path:
    """Write one narration wav whose length matches the storyboard."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        raise TtsError("ffmpeg and ffprobe are required to build the speech track")
    audio_dir = out_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    spoken = [speakable(beat.narration) for beat in board.beats]
    sources = [audio_dir / f"beat-{index:02d}.mp3" for index in range(1, len(board.beats) + 1)]
    synthesize(spoken, sources, voice or _voice())
    _cover_speech(board, sources, max_seconds)
    fitted: list[Path] = []
    for index, (beat, source) in enumerate(zip(board.beats, sources), start=1):
        wav = audio_dir / f"beat-{index:02d}.wav"
        fit_to_duration(source, wav, beat.duration_s)
        fitted.append(wav)
    track = audio_dir / "narration.wav"
    concat_audio(fitted, track)
    return track


def _cover_speech(board: Storyboard, sources: list[Path], max_seconds: float) -> None:
    """Lengthen a beat when the spoken line would otherwise be sped up."""
    tail = 1.0
    lead = LEAD_IN_MS / 1000
    for beat, source in zip(board.beats, sources):
        needed = probe_duration(source) + lead + tail
        if beat.duration_s < needed:
            beat.duration_s = round(needed, 2)
    total = sum(beat.duration_s for beat in board.beats)
    if total <= max_seconds:
        return
    floors = [probe_duration(source) + lead + 0.25 for source in sources]
    overflow = total - max_seconds
    slack = [max(0.0, beat.duration_s - floor) for beat, floor in zip(board.beats, floors)]
    slack_sum = sum(slack)
    if slack_sum <= overflow:
        return
    keep = 1 - overflow / slack_sum
    for beat, floor, extra in zip(board.beats, floors, slack):
        beat.duration_s = round(floor + extra * keep, 2)


def speakable(text: str) -> str:
    """Soften paths and units so the voice reads the script cleanly."""
    spoken = text.strip()
    spoken = re.sub(r"/s\b", " per second", spoken)
    spoken = re.sub(r"\.py\b", " dot py", spoken)
    spoken = re.sub(r"\.md\b", " dot md", spoken)
    spoken = spoken.replace("_", " ")
    spoken = spoken.replace("/", " ")
    spoken = spoken.replace("=", " equals ")
    spoken = re.sub(r"\bms\b", "milliseconds", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return spoken


def synthesize(texts: list[str], paths: list[Path], voice: str) -> None:
    try:
        import edge_tts
    except ImportError as exc:
        raise TtsError("edge-tts is not installed") from exc

    async def save_all() -> None:
        for text, path in zip(texts, paths):
            if not text:
                raise TtsError("a beat has no narration to speak")
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))

    try:
        asyncio.run(save_all())
    except TtsError:
        raise
    except Exception as exc:
        raise TtsError(f"speech synthesis failed: {exc}") from exc
    missing = [path for path in paths if not path.is_file() or path.stat().st_size == 0]
    if missing:
        raise TtsError("speech synthesis wrote no audio")


def fit_to_duration(source: Path, dest: Path, duration: float) -> None:
    """Pad or gently speed the clip so it occupies exactly `duration` seconds."""
    spoken = probe_duration(source)
    room = max(0.35, duration - LEAD_IN_MS / 1000)
    filters = ["aformat=sample_fmts=s16:channel_layouts=mono", "aresample=24000"]
    filters.extend(_atempo_filters(spoken / room if spoken > room else 1.0))
    filters.append(f"adelay={LEAD_IN_MS}:all=1")
    filters.append(f"apad=whole_dur={duration:.3f}")
    _ffmpeg(
        [
            "-i",
            str(source),
            "-af",
            ",".join(filters),
            "-t",
            f"{duration:.3f}",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(dest),
        ]
    )


def concat_audio(clips: list[Path], dest: Path) -> None:
    listing = dest.with_suffix(".concat.txt")
    lines = [f"file '{clip.resolve()}'" for clip in clips]
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(dest)])


def probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise TtsError("ffprobe is required to build the speech track")
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise TtsError(f"ffprobe failed: {detail}") from exc
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise TtsError(f"ffprobe returned no duration for {path}") from exc


def _atempo_filters(tempo: float) -> list[str]:
    if tempo <= 1.01:
        return []
    filters: list[str] = []
    remaining = tempo
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    filters.append(f"atempo={remaining:.4f}")
    return filters


def _voice() -> str:
    import os

    return os.environ.get("DEVVIDEO_TTS_VOICE") or DEFAULT_VOICE


def _ffmpeg(args: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TtsError("ffmpeg is required to build the speech track")
    try:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise TtsError(f"ffmpeg failed: {detail}") from exc
