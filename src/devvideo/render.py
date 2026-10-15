"""Render a storyboard to frames and assemble an mp4.

On-screen code and quotes are the retrieved evidence rows, not model text.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont

from devvideo.evidence import VISIBLE_SINGLE, Evidence, display_rows
from devvideo.script import Storyboard


WIDTH = 1280
HEIGHT = 720
MARGIN = 48

BG = (16, 18, 24)
PANEL = (24, 28, 38)
LINE = (54, 62, 78)
TEXT = (236, 240, 246)
MUTED = (154, 164, 180)
CODE = (214, 222, 234)
LINENO = (118, 128, 146)

SECTION_COLOR = {
    "title": (126, 224, 176),
    "problem": (255, 196, 87),
    "api": (110, 168, 254),
    "how": (186, 158, 255),
    "results": (255, 138, 128),
}
SECTION_LABEL = {
    "title": "Title",
    "problem": "Problem",
    "api": "APIs",
    "how": "How to use it",
    "results": "Results",
}

SANS = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)
SANS_BOLD = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
MONO = (
    "/System/Library/Fonts/Supplemental/Andale Mono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)


class RenderError(RuntimeError):
    pass


def render_video(
    board: Storyboard,
    cards: list[Evidence],
    out_dir: Path,
    audio: Path | None = None,
) -> Path:
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    by_id = {card.id: card for card in cards}
    frames = render_frames(board, by_id, frames_dir)
    video = out_dir / "developer-video.mp4"
    assemble(frames, video, audio=audio)
    return video


def render_frames(
    board: Storyboard,
    by_id: dict[str, Evidence],
    frames_dir: Path,
) -> list[tuple[Path, float]]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    fonts = Fonts.load()
    cursor = 0.0
    frames: list[tuple[Path, float]] = []
    for index, beat in enumerate(board.beats, start=1):
        image = Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = ImageDraw.Draw(image)
        _draw_beat(draw, board, by_id, beat, index, cursor, fonts)
        path = frames_dir / f"beat-{index:02d}-{beat.section}.png"
        image.save(path)
        frames.append((path, beat.duration_s))
        cursor += beat.duration_s
    return frames


def assemble(frames: list[tuple[Path, float]], out: Path, audio: Path | None = None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RenderError("ffmpeg is required to assemble the video")
    if not frames:
        raise RenderError("no frames to assemble")
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    filters = []
    labels = []
    for index, (path, duration) in enumerate(frames):
        command += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(path)]
        filters.append(f"[{index}:v]scale={WIDTH}:{HEIGHT},setsar=1,fps=30,format=yuv420p[v{index}]")
        labels.append(f"[v{index}]")
    filters.append(f"{''.join(labels)}concat=n={len(frames)}:v=1:a=0[v]")
    if audio is not None:
        command += ["-i", str(audio)]
    command += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[v]",
    ]
    if audio is not None:
        command += ["-map", f"{len(frames)}:a:0", "-c:a", "aac", "-b:a", "160k", "-shortest"]
    command += [
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RenderError(f"ffmpeg failed: {detail}") from exc


class Fonts:
    def __init__(self, regular, bold, mono):
        self.regular = regular
        self.bold = bold
        self.mono_path = mono

    @classmethod
    def load(cls) -> "Fonts":
        return cls(_font(SANS, 22), _font(SANS_BOLD, 36), _first_existing(MONO))

    def mono(self, size: int) -> ImageFont.ImageFont:
        if self.mono_path:
            return ImageFont.truetype(self.mono_path, size=size)
        return ImageFont.load_default()


def _draw_beat(draw, board, by_id, beat, index, start, fonts: Fonts) -> None:
    color = SECTION_COLOR[beat.section]
    draw.rectangle((0, 0, 10, HEIGHT), fill=color)
    label = SECTION_LABEL[beat.section]
    draw.text((MARGIN, 28), "DEVELOPER VIDEO", font=_font(SANS_BOLD, 16), fill=color)
    meta = f"{board.repo}    {_clock(start)} - {_clock(start + beat.duration_s)}"
    _draw_right(draw, meta, 28, _font(SANS, 18), MUTED)
    draw.text((MARGIN, 58), label.upper(), font=_font(SANS, 16), fill=MUTED)
    draw.line((MARGIN, 88, WIDTH - MARGIN, 88), fill=LINE, width=1)

    _draw_wrapped(draw, beat.headline, (MARGIN, 104), _font(SANS_BOLD, 34), TEXT, WIDTH - 2 * MARGIN, 2, 40)
    _draw_wrapped(draw, beat.narration, (MARGIN, 196), fonts.regular, TEXT, WIDTH - 2 * MARGIN, 4, 30)

    shown = _cards_for_beat(beat, by_id)
    _draw_panel(draw, shown, by_id, beat, fonts)
    _draw_rail(draw, beat.section, index, len(board.beats))


def _cards_for_beat(beat, by_id: dict[str, Evidence]) -> list[Evidence]:
    cited = [by_id[item] for item in beat.evidence_ids if item in by_id]
    if beat.section == "api":
        symbols = [card for card in cited if card.kind in {"class", "function"}]
        return (symbols or cited)[:3]
    preferred = {"how": "example", "results": "result", "problem": "quote"}.get(beat.section)
    if preferred:
        match = [card for card in cited if card.kind == preferred]
        if match:
            return match[:1]
    return cited[:1]


def _draw_panel(draw, shown: list[Evidence], by_id, beat, fonts: Fonts) -> None:
    top, bottom = 316, 646
    draw.rounded_rectangle((MARGIN, top, WIDTH - MARGIN, bottom), radius=12, fill=PANEL, outline=LINE, width=1)
    if not shown:
        draw.text((MARGIN + 20, top + 20), "No retrieved evidence for this beat.", font=_font(SANS, 20), fill=MUTED)
        return
    hidden = [card for card in (by_id[item] for item in beat.evidence_ids if item in by_id) if card not in shown]
    inner_top = top + 16
    if hidden:
        chip = "Also cited  " + "   ".join(f"{card.id} {card.path}" for card in hidden[:3])
        draw.text((MARGIN + 20, bottom - 28), _fit(draw, chip, _font(SANS, 15), WIDTH - 2 * MARGIN - 40), font=_font(SANS, 15), fill=MUTED)
        bottom -= 24
    slots = len(shown)
    gap = 10
    height = (bottom - inner_top - gap * (slots - 1)) / slots
    limit = 4 if slots > 1 else VISIBLE_SINGLE
    for slot, card in enumerate(shown):
        y = inner_top + slot * (height + gap)
        _draw_card(draw, card, y, y + height, fonts, limit)


def _draw_card(draw, card: Evidence, top: float, bottom: float, fonts: Fonts, limit: int) -> None:
    rows = display_rows(card, limit)
    header = f"RETRIEVED   {card.id}    {_locator(card, rows)}"
    draw.text((MARGIN + 20, top), header, font=_font(SANS_BOLD, 15), fill=SECTION_COLOR.get("title", TEXT))
    if not rows:
        return
    body_top = top + 22
    body_width = WIDTH - 2 * MARGIN - 40
    size = _mono_size(draw, rows, body_width - 64, fonts)
    if limit <= 4:
        size = min(size, 15)
    mono = fonts.mono(size)
    line_height = size + 4
    y = body_top
    for line_no, line in rows:
        if y + line_height > bottom:
            break
        gutter = f"{line_no:>4}"
        draw.text((MARGIN + 20, y), gutter, font=mono, fill=LINENO)
        fitted = _fit(draw, line.replace("\t", "    "), mono, body_width - 64)
        draw.text((MARGIN + 78, y), fitted, font=mono, fill=CODE)
        y += line_height


def _draw_rail(draw, current: str, index: int, total: int) -> None:
    labels = list(SECTION_LABEL)
    gap = 12
    width = (WIDTH - 2 * MARGIN - gap * (len(labels) - 1)) / len(labels)
    y = 658
    for slot, section in enumerate(labels):
        x = MARGIN + slot * (width + gap)
        active = section == current
        color = SECTION_COLOR[section]
        fill = color if active else BG
        outline = color if active else LINE
        draw.rounded_rectangle((x, y, x + width, y + 36), radius=8, fill=fill, outline=outline, width=2)
        ink = BG if active else MUTED
        _draw_center(draw, SECTION_LABEL[section], x, x + width, y + 8, _font(SANS_BOLD, 16), ink)
    draw.text((WIDTH - MARGIN - 70, 58), f"{index}/{total}", font=_font(SANS, 16), fill=MUTED)


def _locator(card: Evidence, rows: list[tuple[int, str]]) -> str:
    if not rows:
        return f"{card.path}:{card.start_line}"
    first, last = rows[0][0], rows[-1][0]
    if first == last:
        return f"{card.path}:{first}"
    return f"{card.path}:{first}-{last}"


def _mono_size(draw, rows, width: int, fonts: Fonts) -> int:
    for size in (18, 16, 15, 14, 13):
        font = fonts.mono(size)
        if all(draw.textlength(line.replace("\t", "    "), font=font) <= width for _, line in rows):
            return size
    return 13


def _draw_wrapped(draw, text, origin, font, fill, max_width, max_lines, line_gap) -> None:
    lines = _wrap(draw, text, font, max_width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + " …", font, max_width)
    x, y = origin
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_gap


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if draw.textlength(trial, font=font) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _fit(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed and draw.textlength(trimmed + ellipsis, font=font) > width:
        trimmed = trimmed[:-1]
    return trimmed + ellipsis if trimmed else ellipsis


def _draw_right(draw, text, y, font, fill) -> None:
    width = draw.textlength(text, font=font)
    draw.text((WIDTH - MARGIN - width, y), text, font=font, fill=fill)


def _draw_center(draw, text, left, right, y, font, fill) -> None:
    width = draw.textlength(text, font=font)
    draw.text((left + (right - left - width) / 2, y), text, font=font, fill=fill)


def _clock(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _font(candidates: tuple[str, ...], size: int) -> ImageFont.ImageFont:
    path = _first_existing(candidates)
    if path:
        return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _first_existing(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None
