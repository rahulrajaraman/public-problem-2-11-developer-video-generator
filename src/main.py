"""Generate a grounded developer video from a GitHub repository."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from devvideo.clone import CloneError
from devvideo.pipeline import run
from devvideo.render import RenderError
from devvideo.script import ScriptError
from devvideo.tts import TtsError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clone a GitHub repo, retrieve README and code evidence, "
            "write a cited developer script, read it aloud, and render a video under one minute."
        )
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub URL, owner/name, or a local checkout",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="output directory (default: artifacts/<repo-slug>)",
    )
    parser.add_argument(
        "--llm",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto uses claude -p when CLAUDE_KEY is set, else an OpenAI-compatible chat model or Ollama; off uses the grounded composer",
    )
    parser.add_argument(
        "--tts",
        choices=("auto", "on", "off"),
        default="auto",
        help="auto reads the narration with edge-tts when the service is reachable",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=58.0,
        help="upper bound for the video, kept under one minute",
    )
    args = parser.parse_args(argv)
    if args.max_seconds <= 0 or args.max_seconds > 60:
        parser.error("--max-seconds must be between 0 and 60")
    out = args.out
    if out is None:
        from devvideo.clone import parse_github

        parsed = parse_github(args.repo)
        slug = parsed[1] if parsed else Path(args.repo).expanduser().name
        out = Path("artifacts") / slug
    try:
        run(args.repo, out, llm=args.llm, tts=args.tts, max_seconds=args.max_seconds)
    except (CloneError, ScriptError, RenderError, TtsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
