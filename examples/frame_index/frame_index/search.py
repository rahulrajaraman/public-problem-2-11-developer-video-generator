"""Sample frame times and rank them for a text query.

`search` uses token overlap as a stand-in for an embedding model.
"""

from dataclasses import dataclass
import re


@dataclass
class Hit:
    frame_index: int
    timestamp_s: float
    score: float


@dataclass
class Index:
    path: str
    fps: float
    timestamps_s: list[float]


def index_video(path: str, duration_s: float, fps: float = 1.0) -> Index:
    """Return one timestamp per frame sampled at `fps` across `duration_s`."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    if duration_s < 0:
        raise ValueError("duration_s must be >= 0")
    count = int(duration_s * fps)
    timestamps = [round(i / fps, 3) for i in range(count)]
    return Index(path=path, fps=fps, timestamps_s=timestamps)


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def search(index: Index, query: str, k: int = 5) -> list[Hit]:
    """Return up to `k` frames, highest token-overlap first.

    Overlap is a placeholder score so callers can exercise the API
    before a vision-language embedding is connected.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    query_tokens = _tokens(query)
    hits = []
    for frame_index, timestamp_s in enumerate(index.timestamps_s):
        caption = f"frame {frame_index} at {timestamp_s} from {index.path}"
        overlap = query_tokens & _tokens(caption)
        score = len(overlap) / max(1, len(query_tokens))
        hits.append(Hit(frame_index=frame_index, timestamp_s=timestamp_s, score=score))
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:k]
