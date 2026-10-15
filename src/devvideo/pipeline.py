"""Run the four roles: retrieve, write, review, produce."""

from __future__ import annotations

from pathlib import Path

from devvideo.clone import RepoRef
from devvideo.roles import agree_on_script, produce, retrieve


def run(
    repo: str,
    out_dir: Path,
    *,
    llm: str = "auto",
    tts: str = "auto",
    max_seconds: float = 58.0,
) -> dict:
    ref, cards = retrieve(repo, out_dir)
    board = agree_on_script(cards, ref.slug, llm=llm, max_seconds=max_seconds)
    result = produce(board, cards, out_dir, tts=tts, max_seconds=max_seconds)
    _print_summary(ref, cards, board, result["video"], result["audio"])
    return result


def _print_summary(ref: RepoRef, cards, board, video: Path, audio: Path | None) -> None:
    print(f"repo: {ref.origin}")
    print(f"evidence: {len(cards)} cards")
    print(f"script: {board.generator}, {len(board.beats)} beats, {sum(b.duration_s for b in board.beats):.1f}s")
    if board.llm_error:
        print(f"llm fallback: {board.llm_error}")
    if board.review_attempts > 1:
        print(f"reviewer: accepted on attempt {board.review_attempts}")
    for beat in board.beats:
        cited = ", ".join(beat.evidence_ids)
        print(f"  [{beat.section:8} {beat.duration_s:4.1f}s] {beat.headline}")
        print(f"    {beat.narration}")
        print(f"    evidence: {cited}")
    print(f"video: {video}")
