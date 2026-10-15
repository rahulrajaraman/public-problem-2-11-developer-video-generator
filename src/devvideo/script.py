"""Build a five-beat storyboard whose claims stay inside retrieved evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import re
import subprocess
from urllib import error, request

from devvideo.evidence import VISIBLE_SINGLE, Evidence, display_rows, has_table, usage_rank


SECTIONS = ("title", "problem", "api", "how", "results")
NUMBER = re.compile(r"\d+(?:\.\d+)?%|\d+\.\d+|\b\d{2,}\b")

SYSTEM = """You write a developer video script shorter than one minute.
Use only the evidence cards in the user message.
Every beat must cite one or more evidence ids from that list.
Do not invent APIs, metrics, file paths, or version numbers.
Any number or percentage in a headline or narration must appear verbatim in the cited cards.
Narration is spoken aloud: plain sentences, no markdown, at most 220 characters.
The how beat headline is "How to use it" and must cite a quick start, usage example, or main entry that shows how to run the project.
Return JSON only. Use exactly five beats, in this order: title, problem, api, how, results.
Each beat has section, headline, narration, and evidence_ids.
"""


class ScriptError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Beat:
    section: str
    headline: str
    narration: str
    evidence_ids: list[str]
    duration_s: float = 8.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Storyboard:
    title: str
    repo: str
    beats: list[Beat]
    generator: str
    llm_error: str | None = None
    review_attempts: int = 1

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["beats"] = [beat.to_dict() for beat in self.beats]
        payload["duration_s"] = round(sum(beat.duration_s for beat in self.beats), 2)
        return payload


@dataclass
class LlmConfig:
    base_url: str
    api_key: str
    model: str


def generate_script(
    cards: list[Evidence],
    repo_name: str,
    *,
    llm: str = "auto",
    max_seconds: float = 58.0,
    feedback: list[str] | None = None,
) -> Storyboard:
    """One scriptwriter draft. The reviewer decides whether a second draft is needed."""
    if not cards:
        raise ScriptError(["no evidence was retrieved from the repository"])
    if llm == "off":
        return compose_grounded(cards, repo_name, max_seconds=max_seconds)
    if os.environ.get("CLAUDE_KEY", "").strip():
        return draft_from_claude(cards, repo_name, feedback=feedback, max_seconds=max_seconds)
    config = resolve_llm()
    if config is not None:
        return draft_from_chat(cards, repo_name, config, feedback=feedback, max_seconds=max_seconds)
    if llm == "on":
        raise ScriptError(
            ["--llm on requires CLAUDE_KEY for claude -p, or DEVVIDEO_LLM_API_KEY / OPENAI_API_KEY, or a running Ollama server"]
        )
    return compose_grounded(cards, repo_name, max_seconds=max_seconds)


def compose_grounded(cards: list[Evidence], repo_name: str, max_seconds: float = 58.0) -> Storyboard:
    by_id = {card.id: card for card in cards}
    title_card = _title_card(cards)
    problem_card = _best(cards, ("problem", "overview", "introduction", "why"), skip={id(title_card)}) or title_card
    if problem_card.kind != "quote":
        quote = _best(cards, ("avoid", "without", "instead", "problem"), kinds={"quote"})
        if quote is not None:
            problem_card = quote
    api_cards = _api_cards(cards)
    example_card = _usage_example(cards)
    main_card = _main_entry(cards, example_card)
    usage_card = _section_for_example(cards, example_card)
    result_card = _result_card(cards) or title_card

    title_name = _repo_title(title_card, repo_name)
    title_text = _spoken_source(title_card)
    problem_text = _spoken_source(problem_card)
    title_sentence = _first_sentence(title_text) or _clip(title_text, 180)
    problem_sentence = _pick_sentence(
        problem_text,
        ("avoid", "without", "instead", "problem", "need", "search", "retriev"),
        avoid={title_sentence},
    ) or _next_sentence(problem_text, title_sentence)

    if api_cards:
        names = ", ".join(card.symbol or card.title for card in api_cards)
        paths = ", ".join(_unique(card.path for card in api_cards)[:2])
        api_narration = f"Use {names}. Signatures are retrieved from {paths}."
        api_headline = "APIs: " + ", ".join(card.symbol or card.title for card in api_cards[:3])
        api_ids = [card.id for card in api_cards]
    else:
        api_narration = f"No public signatures were retrieved. Closest evidence is {title_card.path}."
        api_headline = "APIs"
        api_ids = [title_card.id]

    shown = example_card or main_card or usage_card or (api_cards[0] if api_cards else title_card)
    how_ids = [card.id for card in (example_card, main_card, usage_card) if card is not None and card.id not in {shown.id}]
    how_ids.insert(0, shown.id)
    how_narration = _usage_narration(shown)

    result_narration = _result_narration(result_card)
    beats = [
        Beat("title", _clip(title_name, 80), _clip(title_sentence, 220), [title_card.id]),
        Beat("problem", "The problem", _clip(problem_sentence, 220), [problem_card.id]),
        Beat("api", _clip(api_headline, 80), _clip(api_narration, 220), api_ids),
        Beat("how", "How to use it", _clip(how_narration, 220), how_ids),
        Beat("results", "Results", _clip(result_narration, 220), [result_card.id]),
    ]
    _assign_durations(beats, max_seconds)
    board = Storyboard(title=title_name, repo=repo_name, beats=beats, generator="grounded")
    errors = validate_storyboard(board, by_id, max_seconds=max_seconds)
    if errors:
        raise ScriptError(errors)
    return board


def validate_storyboard(
    board: Storyboard,
    by_id: dict[str, Evidence],
    max_seconds: float = 58.0,
) -> list[str]:
    errors: list[str] = []
    if [beat.section for beat in board.beats] != list(SECTIONS):
        errors.append("beats must be exactly title, problem, api, how, results")
    total = 0.0
    for beat in board.beats:
        total += beat.duration_s
        if not beat.evidence_ids:
            errors.append(f"{beat.section} cites no evidence")
        if len(beat.headline) > 80:
            errors.append(f"{beat.section} headline is longer than 80 characters")
        if len(beat.narration) > 280:
            errors.append(f"{beat.section} narration is longer than 280 characters")
        missing = [item for item in beat.evidence_ids if item not in by_id]
        if missing:
            errors.append(f"{beat.section} cites unknown evidence {', '.join(missing)}")
            continue
        blob = "\n".join(by_id[item].text for item in beat.evidence_ids)
        for label, text in (("headline", beat.headline), ("narration", beat.narration)):
            for number in NUMBER.findall(text):
                if number not in blob:
                    errors.append(f"{beat.section} {label} number {number} is not in the cited evidence")
    if total > max_seconds + 0.05:
        errors.append(f"duration {total:.1f}s exceeds {max_seconds:.0f}s")
    return errors


def resolve_llm() -> LlmConfig | None:
    key = os.environ.get("DEVVIDEO_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if key:
        base = (
            os.environ.get("DEVVIDEO_LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        model = os.environ.get("DEVVIDEO_LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
        return LlmConfig(base_url=base.rstrip("/"), api_key=key, model=model)
    return _ollama_config()


def _ollama_config() -> LlmConfig | None:
    try:
        with request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, error.URLError):
        return None
    models = payload.get("models") or []
    if not models:
        return None
    preferred = os.environ.get("DEVVIDEO_LLM_MODEL")
    name = preferred or models[0].get("name")
    if not name:
        return None
    return LlmConfig(base_url="http://127.0.0.1:11434/v1", api_key="ollama", model=name)


def draft_from_claude(
    cards: list[Evidence],
    repo_name: str,
    *,
    feedback: list[str] | None = None,
    max_seconds: float = 58.0,
    runner=None,
) -> Storyboard:
    key = os.environ.get("CLAUDE_KEY", "").strip()
    if not key:
        raise ScriptError(["CLAUDE_KEY is not set"])
    raw = complete_claude(SYSTEM, _prompt_with_feedback(cards, repo_name, feedback), key, runner=runner)
    return _board_from_model_text(raw, repo_name, "claude", max_seconds)


def draft_from_chat(
    cards: list[Evidence],
    repo_name: str,
    config: LlmConfig,
    *,
    feedback: list[str] | None = None,
    max_seconds: float = 58.0,
) -> Storyboard:
    raw = complete_chat(config, SYSTEM, _prompt_with_feedback(cards, repo_name, feedback))
    return _board_from_model_text(raw, repo_name, f"llm:{config.model}", max_seconds)


def _prompt_with_feedback(cards: list[Evidence], repo_name: str, feedback: list[str] | None) -> str:
    prompt = _prompt(cards, repo_name)
    if feedback:
        prompt += "\nFix these errors and return JSON only:\n- " + "\n- ".join(feedback)
    return prompt


def _board_from_model_text(raw: str, repo_name: str, generator: str, max_seconds: float) -> Storyboard:
    try:
        board = _parse_board(raw, repo_name)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ScriptError([f"invalid JSON ({exc})"]) from exc
    _assign_durations(board.beats, max_seconds)
    board.generator = generator
    return board


def complete_claude(system: str, user: str, key: str, runner=None) -> str:
    """Ask claude -p for one draft. CLAUDE_KEY is a Claude login token, not an API key."""
    if runner is None:
        runner = subprocess.run
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = key
    try:
        completed = runner(
            [
                "claude",
                "-p",
                "--output-format",
                "text",
                "--tools",
                "",
                "--no-session-persistence",
                "--disable-slash-commands",
                "--system-prompt",
                system,
            ],
            input=user,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ScriptError(["claude is not installed"]) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScriptError(["claude -p timed out"]) from exc
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if getattr(completed, "returncode", 1) != 0:
        detail = stderr.strip() or stdout.strip() or "claude -p failed"
        raise ScriptError([_redact(detail[:400], key)])
    if not stdout.strip():
        raise ScriptError(["claude -p returned an empty reply"])
    return stdout


def _redact(text: str, key: str) -> str:
    return text.replace(key, "[redacted]") if key else text


def complete_chat(config: LlmConfig, system: str, user: str) -> str:
    body = {
        "model": config.model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        f"{config.base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ScriptError([f"LLM request failed ({exc.code}): {detail}"]) from exc
    except (OSError, json.JSONDecodeError, error.URLError) as exc:
        raise ScriptError([f"LLM request failed: {exc}"]) from exc
    try:
        return parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ScriptError(["LLM response did not include a message"]) from exc


def _prompt(cards: list[Evidence], repo_name: str) -> str:
    packed = [
        {
            "id": card.id,
            "kind": card.kind,
            "path": card.path,
            "lines": [card.start_line, card.end_line],
            "title": card.title,
            "symbol": card.symbol,
            "text": card.text[:1500],
        }
        for card in cards
    ]
    schema = {
        "title": "short video title",
        "beats": [
            {
                "section": section,
                "headline": "on-screen heading",
                "narration": "spoken line grounded in the cited cards",
                "evidence_ids": ["E1"],
            }
            for section in SECTIONS
        ],
    }
    return (
        f"Repository: {repo_name}\n\n"
        f"Evidence cards:\n{json.dumps(packed, indent=2)}\n\n"
        f"JSON shape:\n{json.dumps(schema, indent=2)}"
    )


def _parse_board(raw: str, repo_name: str) -> Storyboard:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object")
    payload = json.loads(raw[start : end + 1])
    beats = []
    for item in payload["beats"]:
        ids = item["evidence_ids"]
        if isinstance(ids, str):
            ids = [ids]
        section = str(item["section"]).strip().lower()
        headline = "How to use it" if section == "how" else str(item["headline"]).strip()
        beats.append(
            Beat(
                section=section,
                headline=headline,
                narration=str(item["narration"]).strip(),
                evidence_ids=[str(value) for value in ids],
            )
        )
    title = str(payload.get("title") or beats[0].headline).strip()
    return Storyboard(title=title, repo=repo_name, beats=beats, generator="llm")


def _assign_durations(beats: list[Beat], max_seconds: float) -> None:
    raw: list[float] = []
    for beat in beats:
        words = max(1, len(beat.narration.split()))
        raw.append(min(13.0, max(7.0, words / 2.4 + 4.0)))
    total = sum(raw)
    if total > max_seconds:
        raw = [value * (max_seconds / total) for value in raw]
    elif total < min(46.0, max_seconds):
        target = min(46.0, max_seconds)
        scaled = [min(14.0, value * (target / total)) for value in raw]
        if sum(scaled) <= max_seconds:
            raw = scaled
    if sum(raw) > max_seconds:
        raw = [value * (max_seconds / sum(raw)) for value in raw]
    for beat, duration in zip(beats, raw):
        beat.duration_s = round(duration, 2)


def _spoken_source(card: Evidence) -> str:
    lines = []
    for _, line in card.rows:
        if line.count("](") >= 2 or "<sup" in line.lower() or "&nbsp;" in line:
            continue
        if re.search(r"<img\b|<div\b|</div>|<p\b|</p>|<br\b|<a\b", line, re.I):
            continue
        stripped = re.sub(r"^>\s?", "", line.strip()).replace("`", "")
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        stripped = stripped.replace("&nbsp;", " ").replace("&amp;", "&")
        stripped = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", stripped)
        stripped = stripped.replace("**", "").replace("__", "")
        stripped = stripped.replace("*", "")
        stripped = " ".join(stripped.split())
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("English ") or (stripped.startswith("[") and "](" in stripped and len(stripped) < 140):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip() or card.text.replace("`", "")


_SKIP_CALLS = {
    "print", "len", "range", "open", "set", "list", "dict", "str", "int", "float",
    "super", "filterwarnings", "setLevel", "getLogger", "set_start_method", "getenv",
}


def _usage_example(cards: list[Evidence]) -> Evidence | None:
    examples = [
        card
        for card in cards
        if card.kind == "example" and card.title != "main entry" and not re.search(r"@\w+\{", card.text)
    ]
    return max(examples, key=usage_rank) if examples else None


def _main_entry(cards: list[Evidence], usage: Evidence | None) -> Evidence | None:
    mains = [
        card
        for card in cards
        if (card.title == "main entry" or card.symbol == "main") and (usage is None or card.id != usage.id)
    ]
    if usage is not None:
        usage_calls = set(re.findall(r"\b([A-Za-z_]\w*)\(", usage.text))
        related = [
            card
            for card in mains
            if usage_calls & set(re.findall(r"\b([A-Za-z_]\w*)\(", card.text))
        ]
        mains = related
    return max(mains, key=usage_rank) if mains else None


def _usage_narration(card: Evidence) -> str:
    calls: list[str] = []
    for name in re.findall(r"\b([A-Za-z_]\w*)\(", card.text):
        if name in _SKIP_CALLS or name.startswith("_") or name in calls:
            continue
        calls.append(name)
    if calls:
        return f"Call {', '.join(calls[:3])}. These steps are retrieved from {card.path}."
    scripts: list[str] = []
    for name in re.findall(r"\b([\w./-]+\.(?:bash|sh|py))\b", card.text):
        base = Path(name).name
        if base not in scripts:
            scripts.append(base)
    if scripts:
        steps = ", then ".join(scripts[:2])
        return f"Run {steps}. These steps are retrieved from {card.path}."
    return f"Follow the usage steps retrieved from {card.path}."


def _section_for_example(cards: list[Evidence], example: Evidence | None) -> Evidence | None:
    if example is None or not example.title.endswith(" example"):
        return None
    heading = example.title[: -len(" example")]
    for card in cards:
        if card.path == example.path and card.title == heading and card.kind != "example":
            return card
    return None


def _result_card(cards: list[Evidence]) -> Evidence | None:
    titled = [card for card in cards if card.kind == "result"]
    pool = titled or [card for card in cards if has_table(card.text)]
    tables = [card for card in pool if has_table(card.text)]
    if tables:
        return max(tables, key=lambda card: (card.text.count("|"), card.score))
    if pool:
        return max(pool, key=lambda card: card.score)
    return _best(cards, ("result", "benchmark", "evaluation", "accuracy", "recall"))


def _result_narration(card: Evidence) -> str:
    rows = display_rows(card, VISIBLE_SINGLE)
    claim = _table_claim(rows)
    if claim and _numbers_ok(claim, card.text):
        return claim
    visible = _spoken_source(card)
    sentence = _pick_sentence(
        visible,
        ("result", "benchmark", "%", "recall", "accuracy", "timestamp", "hit"),
        avoid=set(),
        prefer_numbers=True,
    )
    if sentence:
        return sentence
    return f"No benchmark figure was retrieved. Closest evidence is {card.path}."


def _table_claim(rows: list[tuple[int, str]]) -> str | None:
    parsed: list[list[str]] = []
    for _, line in rows:
        stripped = line.strip()
        if stripped.startswith("|"):
            parsed.append([cell.strip() for cell in stripped.strip("|").split("|")])
    if len(parsed) < 2:
        return None
    header = [cell.lower() for cell in parsed[0]]
    best: tuple[int, str] | None = None
    for cells in parsed[1:]:
        if all(set(cell) <= set("-: ") or not cell for cell in cells):
            continue
        data = {name: value for name, value in zip(header, cells) if name and value}
        decisions = _first_column(data, "decisions", "decision")
        path = _first_column(data, "path")
        total = _first_column(data, "total ms")
        rate = data.get("requested decisions/s") or _first_column(data, "decisions/s")
        if not (decisions and path and total):
            continue
        noun = "decision" if decisions == "1" else "decisions"
        claim = f"{decisions} {noun} on the {path} path: {total} total ms"
        if rate:
            claim += f", {rate} requested decisions/s"
        rank = (10 if path.lower() == "shared" else 0) + _int_prefix(decisions)
        if best is None or rank > best[0]:
            best = (rank, claim + ".")
    return None if best is None else best[1]


def _first_column(data: dict[str, str], *needles: str) -> str | None:
    for needle in needles:
        if needle in data:
            return data[needle]
    return None


def _int_prefix(value: str) -> int:
    match = re.match(r"\d+", value)
    return int(match.group(0)) if match else 0


def _numbers_ok(text: str, blob: str) -> bool:
    return all(number in blob for number in NUMBER.findall(text))


def _title_card(cards: list[Evidence]) -> Evidence:
    root = [
        card
        for card in cards
        if card.kind == "readme_section" and "/" not in card.path and card.path.lower().startswith("readme")
    ]
    if root:
        return min(root, key=lambda card: card.start_line)
    return cards[0]


def _section_mentioning(cards: list[Evidence], example: Evidence | None) -> Evidence | None:
    """README section that describes the symbols in a retrieved example."""
    if example is None:
        return None
    names = {name for name in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", example.text) if len(name) > 3}
    best: tuple[int, Evidence] | None = None
    for card in cards:
        if card.kind not in {"readme_section", "quote"} or card.id == example.id:
            continue
        hits = sum(1 for name in names if name in card.text)
        if hits and (best is None or hits > best[0]):
            best = (hits, card)
    return None if best is None else best[1]


def _api_cards(cards: list[Evidence]) -> list[Evidence]:
    """Symbols a caller actually invokes, plus the types those signatures name."""
    helper_dirs = {"examples", "scripts", "demo", "demos", "benchmarks", "tests", "test"}
    symbols = [
        card
        for card in cards
        if card.kind in {"class", "function"}
        and card.symbol
        and not (set(Path(card.path).parts) & helper_dirs)
    ]
    example_text = "\n".join(card.text for card in cards if card.kind == "example")

    def called(card: Evidence) -> bool:
        return re.search(rf"(?<!\.)\b{re.escape(card.symbol)}\s*\(", example_text) is not None

    def call_at(card: Evidence) -> int:
        match = re.search(rf"(?<!\.)\b{re.escape(card.symbol)}\s*\(", example_text)
        return match.start() if match else 10**9

    primary = sorted((card for card in symbols if called(card)), key=call_at)
    related: list[Evidence] = []
    for card in primary:
        for name in re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", card.text):
            match = next((item for item in symbols if item.symbol == name), None)
            if match is not None and match not in primary and match not in related:
                related.append(match)
    rest = sorted(symbols, key=lambda card: card.score, reverse=True)
    picked: list[Evidence] = []
    for card in [*primary, *related, *rest]:
        if card not in picked:
            picked.append(card)
        if len(picked) == 3:
            break
    return picked


def _best(
    cards: list[Evidence],
    keywords: tuple[str, ...],
    kinds: set[str] | None = None,
    skip: set[int] | None = None,
) -> Evidence | None:
    skip = skip or set()
    ranked: list[tuple[float, Evidence]] = []
    for card in cards:
        if kinds is not None and card.kind not in kinds:
            continue
        if id(card) in skip:
            continue
        hay = f"{card.title}\n{card.path}\n{card.text[:400]}".lower()
        hits = sum(1 for word in keywords if word in hay)
        if hits:
            ranked.append((hits * 10 + card.score, card))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _repo_title(card: Evidence, fallback: str) -> str:
    for _, line in card.rows:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
    first = card.title.strip()
    if first and first.lower() not in {"introduction", "note"}:
        return first
    return fallback


def _sentences(text: str) -> list[str]:
    flat = " ".join(text.split())
    parts = re.split(r"(?<=[.!?])\s+", flat)
    return [part.strip() for part in parts if part.strip()]


def _first_sentence(text: str) -> str:
    for sentence in _sentences(text):
        if len(sentence) >= 40:
            return sentence
    flat = " ".join(text.split())
    return flat[:180].strip()


def _next_sentence(text: str, previous: str) -> str:
    found = False
    for sentence in _sentences(text):
        if sentence == previous:
            found = True
            continue
        if found and len(sentence) >= 40:
            return sentence
    return _first_sentence(text)


def _pick_sentence(
    text: str,
    keywords: tuple[str, ...],
    avoid: set[str],
    prefer_numbers: bool = False,
) -> str:
    ranked: list[tuple] = []
    for sentence in _sentences(text):
        if sentence in avoid or not 40 <= len(sentence) <= 220:
            continue
        hits = sum(1 for word in keywords if re.search(rf"\b{re.escape(word)}\b", sentence.lower()))
        numbers = len(NUMBER.findall(sentence)) if prefer_numbers else 0
        ranked.append((hits, numbers, -len(sentence), sentence))
    if not ranked:
        return ""
    ranked.sort(reverse=True)
    return ranked[0][3]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut if cut else text[:limit]


def _unique(items) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def narration_script(board: Storyboard) -> str:
    """Timed narration for a later text-to-speech pass."""
    cursor = 0.0
    blocks: list[str] = []
    for beat in board.beats:
        start = _clock(cursor)
        cursor += beat.duration_s
        end = _clock(cursor)
        blocks.append(f"[{start}-{end}] {beat.section}\n{beat.narration}")
    return "\n\n".join(blocks) + "\n"


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"
