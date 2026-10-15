"""Four roles that hand a repository from checkout to a finished video.

Retriever writes evidence.json.
Scriptwriter writes one storyboard from that pack.
Reviewer accepts it or sends the problems back, once.
Producer renders speech and the mp4 only after the reviewer accepts.
"""

from __future__ import annotations

from pathlib import Path

from devvideo.clone import RepoRef, acquire
from devvideo.evidence import Evidence, extract
from devvideo.render import render_video
from devvideo.script import ScriptError, Storyboard, generate_script, narration_script, validate_storyboard
from devvideo.tts import TtsError, narrate


def retrieve(repo: str, out_dir: Path) -> tuple[RepoRef, list[Evidence]]:
    """Checkout the repo and write the evidence pack. The repo is not executed."""
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
    print(f"retriever: {len(cards)} cards from {ref.origin}")
    return ref, cards


def write_script(
    cards: list[Evidence],
    repo_name: str,
    *,
    llm: str,
    max_seconds: float,
    feedback: list[str] | None = None,
    label: str | None = None,
) -> Storyboard:
    """One draft. CLAUDE_KEY selects claude -p. --llm off stays on the grounded composer."""
    board = generate_script(cards, repo_name, llm=llm, max_seconds=max_seconds, feedback=feedback)
    print(f"scriptwriter: {board.generator} ({label or ('revision' if feedback else 'draft')})")
    return board


def review(board: Storyboard, cards: list[Evidence], max_seconds: float) -> list[str]:
    """Empty list means the producer may start. Problems go back to the scriptwriter."""
    errors = validate_storyboard(board, {card.id: card for card in cards}, max_seconds=max_seconds)
    if errors:
        print("reviewer: rejected")
        for item in errors:
            print(f"  {item}")
        return errors
    print("reviewer: accepted")
    return []


def produce(
    board: Storyboard,
    cards: list[Evidence],
    out_dir: Path,
    *,
    tts: str,
    max_seconds: float,
) -> dict:
    """Speech and frames. Called only after the reviewer accepts the storyboard."""
    audio = _speech_track(board, out_dir, tts, max_seconds)
    _write_json(out_dir / "script.json", board.to_dict())
    (out_dir / "narration.txt").write_text(narration_script(board), encoding="utf-8")
    video = render_video(board, cards, out_dir, audio=audio)
    print(f"producer: {video}")
    return {
        "video": video,
        "evidence": out_dir / "evidence.json",
        "script": out_dir / "script.json",
        "narration": out_dir / "narration.txt",
        "audio": audio,
        "duration_s": sum(beat.duration_s for beat in board.beats),
        "generator": board.generator,
    }


def agree_on_script(
    cards: list[Evidence],
    repo_name: str,
    *,
    llm: str,
    max_seconds: float,
    draft=None,
) -> Storyboard:
    """Scriptwriter drafts, reviewer replies, scriptwriter may revise once."""
    write = draft or (lambda feedback: write_script(
        cards, repo_name, llm=llm, max_seconds=max_seconds, feedback=feedback
    ))
    try:
        board = write(None)
    except ScriptError:
        if llm != "auto":
            raise
        return _grounded_fallback(cards, repo_name, max_seconds, "scriptwriter could not draft")
    errors = review(board, cards, max_seconds)
    if not errors:
        board.review_attempts = 1
        return board
    if board.generator == "grounded":
        raise ScriptError(errors)
    try:
        revised = write(errors)
    except ScriptError as exc:
        if llm != "auto":
            raise
        return _grounded_fallback(cards, repo_name, max_seconds, str(exc))
    errors = review(revised, cards, max_seconds)
    revised.review_attempts = 2
    if not errors:
        return revised
    if llm == "auto":
        return _grounded_fallback(cards, repo_name, max_seconds, "; ".join(errors))
    raise ScriptError(errors)


def _grounded_fallback(cards: list[Evidence], repo_name: str, max_seconds: float, reason: str) -> Storyboard:
    board = write_script(cards, repo_name, llm="off", max_seconds=max_seconds, label="fallback")
    board.llm_error = reason
    board.review_attempts = 2
    errors = review(board, cards, max_seconds)
    if errors:
        raise ScriptError(errors)
    return board


def _speech_track(board: Storyboard, out_dir: Path, tts: str, max_seconds: float) -> Path | None:
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


def _write_json(path: Path, payload: dict) -> None:
    import json

    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
