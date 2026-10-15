"""Clone, retrieve evidence, write a cited script, render the video."""

from __future__ import annotations

import json
from pathlib import Path

from devvideo.clone import RepoRef, acquire
from devvideo.evidence import extract
from devvideo.render import render_video
from devvideo.script import generate_script, narration_script
from devvideo.tts import TtsError, narrate


def run(
    repo: str,
    out_dir: Path,
    *,
    llm: str = "auto",
    tts: str = "auto",
    max_seconds: float = 58.0,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ref = acquire(repo, out_dir)
    cards = extract(ref.path, skip=[out_dir] if _inside(out_dir, ref.path) else None)
    if not cards:
        raise SystemExit(f"no README or Python evidence found in {ref.path}")
    _write_json(out_dir / "evidence.json", {
        "repo": ref.origin,
        "slug": ref.slug,
        "cloned": ref.cloned,
        "cards": [card.to_dict() for card in cards],
    })
    board = generate_script(cards, ref.slug, llm=llm, max_seconds=max_seconds)
    audio = _speech_track(board, out_dir, tts, max_seconds)
    _write_json(out_dir / "script.json", board.to_dict())
    (out_dir / "narration.txt").write_text(narration_script(board), encoding="utf-8")
    video = render_video(board, cards, out_dir, audio=audio)
    _print_summary(ref, cards, board, video, audio)
    return {
        "video": video,
        "evidence": out_dir / "evidence.json",
        "script": out_dir / "script.json",
        "narration": out_dir / "narration.txt",
        "audio": audio,
        "duration_s": sum(beat.duration_s for beat in board.beats),
        "generator": board.generator,
    }


def _speech_track(board, out_dir: Path, tts: str, max_seconds: float) -> Path | None:
    if tts == "off":
        return None
    try:
        track = narrate(board, out_dir, max_seconds=max_seconds)
    except TtsError as exc:
        if tts == "on":
            raise
        print(f"speech skipped: {exc}")
        return None
    print(f"speech: {track}")
    return track


def _print_summary(ref: RepoRef, cards, board, video: Path, audio: Path | None) -> None:
    print(f"repo: {ref.origin}")
    print(f"evidence: {len(cards)} cards")
    print(f"script: {board.generator}, {len(board.beats)} beats, {sum(b.duration_s for b in board.beats):.1f}s")
    if board.llm_error:
        print(f"llm fallback: {board.llm_error}")
    for beat in board.beats:
        cited = ", ".join(beat.evidence_ids)
        print(f"  [{beat.section:8} {beat.duration_s:4.1f}s] {beat.headline}")
        print(f"    {beat.narration}")
        print(f"    evidence: {cited}")
    print(f"video: {video}")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
