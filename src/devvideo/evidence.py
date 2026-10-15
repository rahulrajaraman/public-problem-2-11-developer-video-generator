"""Retrieve grounded evidence cards from a repository.

Cards are slices of README, docs, and public Python symbols. Later stages
may cite these ids. They may not invent replacement text for the video.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re


VISIBLE_SINGLE = 11

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".models",
    "dist",
    "build",
    "site-packages",
    "third_party",
    "vendor",
    "data",
    "datasets",
    "checkpoints",
    "weights",
    "artifacts",
}

KEYWORD_WEIGHTS = (
    ("problem", 3.0),
    ("overview", 2.0),
    ("introduction", 2.0),
    ("why", 1.5),
    ("usage", 3.0),
    ("example", 2.5),
    ("quickstart", 3.0),
    ("getting started", 3.0),
    ("api", 3.0),
    ("inference", 2.0),
    ("how to use", 3.0),
    ("quick start", 3.5),
    ("getting started", 3.0),
    ("result", 3.5),
    ("benchmark", 3.5),
    ("evaluation", 2.5),
    ("metric", 2.0),
)

FENCE = re.compile(r"^[ \t]*```([^\n`]*)\n(.*?)(?:\n[ \t]*```)", re.M | re.S)
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$", re.M)
QUOTE = re.compile(r"(?:^> ?(.*)(?:\n|$))+", re.M)


@dataclass
class Evidence:
    id: str
    kind: str
    path: str
    start_line: int
    end_line: int
    title: str
    text: str
    rows: list[tuple[int, str]] = field(default_factory=list)
    symbol: str | None = None
    score: float = 0.0

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["rows"] = [[line_no, line] for line_no, line in self.rows]
        return payload


def display_rows(card: Evidence, limit: int) -> list[tuple[int, str]]:
    rows = [(line_no, line.rstrip()) for line_no, line in card.rows if line.strip()]
    if card.kind == "example":
        rows = _window_on_pipeline(rows, limit)
    return rows[:limit]


def usage_rank(card: Evidence) -> tuple:
    """Prefer a quick-start or main entry over citations and benchmark scripts."""
    title = card.title.lower()
    usage = 2 if re.search(r"quick\s*start|getting started|usage|how to use|run from source", title) else 0
    if card.symbol == "main" or card.title == "main entry":
        usage += 1
    command = 1 if _looks_like_commands(card.text) else 0
    scripts = len(re.findall(r"\bbash\s+\S+\.(?:bash|sh)\b|\bpython\s+\S+\.py\b", card.text))
    runnable = 1 if re.search(r"^(?:from |import )|if __name__|^\s*(?:python|pip|bash)\b", card.text, re.M) else 0
    citation = 1 if re.search(r"@\w+\{", card.text) else 0
    bench = 1 if re.search(r"reproduce|benchmark|longervideo|evaluation", card.path, re.I) else 0
    return (usage, command, scripts, runnable, -citation, -bench, card.text.count("("), card.score)


def _window_on_pipeline(rows: list[tuple[int, str]], limit: int) -> list[tuple[int, str]]:
    """Keep the entry point and the last call in the same visible window."""
    anchor = next(
        (index for index, (_, line) in enumerate(rows) if re.search(r"if __name__|^\s*(?:python|pip)\b", line)),
        None,
    )
    if anchor is None:
        return rows[:limit]
    call = anchor
    for index in range(anchor, len(rows)):
        if re.search(r"\b[A-Za-z_]\w*\(", rows[index][1]):
            call = index
    start = anchor if call - anchor + 1 <= limit else max(anchor, call - limit + 1)
    return rows[start : start + limit]


def extract(repo: Path, skip: list[Path] | None = None) -> list[Evidence]:
    repo = repo.resolve()
    skip_resolved = {path.resolve() for path in (skip or [])}
    readme = _find_readme(repo)
    readme_text = _read(readme) if readme else ""
    cards: list[Evidence] = []
    if readme is not None:
        cards.extend(_markdown_cards(repo, readme))
    for path in _extra_markdown(repo, readme):
        cards.extend(_markdown_cards(repo, path))
    exported = _exported_names(repo, skip_resolved)
    for path in _python_files(repo, readme_text, skip_resolved):
        cards.extend(_python_cards(repo, path))
    example_text = "\n".join(card.text for card in cards if card.kind == "example")
    corpus = "\n".join(card.text for card in cards if card.kind in {"readme_section", "quote", "example", "result"})
    corpus = f"{readme_text}\n{corpus}"
    for card in cards:
        card.score = _score(card, corpus, example_text, exported)
    _downrank_duplicate_methods(cards)
    return _select(cards, exported)


def _find_readme(repo: Path) -> Path | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = repo / name
        if path.is_file():
            return path
    for path in repo.iterdir():
        if path.is_file() and path.name.lower() == "readme.md":
            return path
    return None


def _extra_markdown(repo: Path, primary: Path | None) -> list[Path]:
    found: list[Path] = []
    for folder in ("docs", "benchmarks", "doc"):
        root = repo / folder
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            if _skipped(path, repo):
                continue
            if path.name.lower().endswith(".zh-cn.md"):
                continue
            if primary is not None and path.resolve() == primary.resolve():
                continue
            found.append(path)
    for path in repo.rglob("README.md"):
        if _skipped(path, repo) or path.stat().st_size > 200_000:
            continue
        if primary is not None and path.resolve() == primary.resolve():
            continue
        if path not in found:
            found.append(path)
    def rank(path: Path) -> tuple[int, int]:
        name = path.as_posix().lower()
        hits = sum(1 for word in ("quick", "usage", "start", "result", "benchmark", "inference", "api") if word in name)
        return (-hits, len(path.relative_to(repo).parts))

    found.sort(key=rank)
    return found[:8]


def _python_files(repo: Path, readme_text: str, skip: set[Path]) -> list[Path]:
    candidates: list[Path] = []
    for path in repo.rglob("*.py"):
        if _skipped(path, repo) or _under(path, skip):
            continue
        if path.stat().st_size > 200_000:
            continue
        parts = set(path.relative_to(repo).parts)
        if parts & {"tests", "test", "examples", "scripts", "demo", "demos", "benchmarks"} or path.name.startswith("test_"):
            continue
        candidates.append(path)

    def rank(path: Path) -> tuple[int, int, str]:
        rel = path.relative_to(repo).as_posix()
        stem = path.stem
        mentioned = 0 if len(stem) >= 4 and re.search(rf"\b{re.escape(stem)}\b", readme_text) else 1
        return (mentioned, len(Path(rel).parts), rel)

    candidates.sort(key=rank)
    return candidates[:40]


def _markdown_cards(repo: Path, path: Path) -> list[Evidence]:
    raw = _read(path)
    if not raw.strip():
        return []
    rel = path.relative_to(repo).as_posix()
    lines = raw.splitlines()
    cards: list[Evidence] = []
    outside = _outside_fences(raw)
    headings = [match for match in HEADING.finditer(raw) if outside(match.start())]
    spans: list[tuple[int, int, str]] = []
    if not headings:
        spans.append((1, len(lines), path.stem))
    else:
        intro_end = _line_of(raw, headings[0].start()) - 1
        if headings[0].start() > 0 and raw[: headings[0].start()].strip() and intro_end >= 1:
            spans.append((1, intro_end, "Introduction"))
        for index, match in enumerate(headings):
            start_line = _line_of(raw, match.start())
            if index + 1 < len(headings):
                end_line = _line_of(raw, headings[index + 1].start()) - 1
            else:
                end_line = _last_line(raw)
            if end_line < start_line:
                end_line = start_line
            spans.append((start_line, end_line, match.group(2).strip()))

    for start_line, end_line, title in spans:
        chunk = lines[start_line - 1 : end_line]
        rows = _prose_rows(chunk, start_line)
        if len(" ".join(line for _, line in rows)) < 40:
            continue
        rows = _cap_rows(rows, 36, 1800)
        kind = _section_kind(title, rel)
        cards.append(_card(kind, rel, title, rows))

    for group in _fence_groups(list(FENCE.finditer(raw)), raw):
        rows: list[tuple[int, str]] = []
        for match in group:
            start_line = raw[: match.start()].count("\n") + 2
            rows.extend((start_line + offset, line.rstrip()) for offset, line in enumerate(match.group(2).splitlines()))
        body = "\n".join(line for _, line in rows)
        if not 20 <= len(body.strip()) <= 1200:
            continue
        rows = _cap_rows(rows, 28, 1200)
        if not rows:
            continue
        heading = _heading_before(raw, group[0].start())
        cards.append(_card("example", rel, f"{heading} example" if heading else "Example", rows))

    for match in QUOTE.finditer(raw):
        if not outside(match.start()):
            continue
        quote = " ".join(line.strip() for line in match.group(0).splitlines())
        quote = re.sub(r"^> ?", "", quote, flags=re.M)
        quote = " ".join(quote.split())
        if len(quote) < 40:
            continue
        start_line = raw[: match.start()].count("\n") + 1
        end_line = raw[: match.end()].count("\n") + 1
        rows = [(start_line, quote[:500])]
        cards.append(_card("quote", rel, "Note", rows, end_line=end_line))
    return cards


def _python_cards(repo: Path, path: Path) -> list[Evidence]:
    source = _read(path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    rel = path.relative_to(repo).as_posix()
    cards: list[Evidence] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            cards.append(_symbol_card(rel, node, kind="function"))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            cards.append(_symbol_card(rel, node, kind="class"))
    cards.extend(_main_cards(source, tree, rel))
    return cards


def _main_cards(source: str, tree: ast.AST, rel: str) -> list[Evidence]:
    """The `if __name__ == "__main__"` block is the runnable pipeline."""
    if re.search(r"reproduce|benchmark|longervideo", rel, re.I):
        return []
    lines = source.splitlines()
    cards: list[Evidence] = []
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        try:
            test = ast.unparse(node.test)
        except Exception:
            continue
        if "__name__" not in test or "__main__" not in test:
            continue
        end = min(node.end_lineno or node.lineno, node.lineno + 21)
        rows = [(number, lines[number - 1].rstrip()) for number in range(node.lineno, end + 1) if number <= len(lines)]
        rows = _cap_rows(rows, 22, 1200)
        if len(rows) < 3:
            continue
        cards.append(_card("example", rel, "main entry", rows, symbol="main"))
    return cards


def _symbol_card(rel: str, node: ast.AST, kind: str) -> Evidence:
    rows: list[tuple[int, str]] = []
    if isinstance(node, ast.ClassDef):
        bases = f"({', '.join(ast.unparse(base) for base in node.bases)})" if node.bases else ""
        rows.append((node.lineno, f"class {node.name}{bases}:"))
        doc = _doc_line(node)
        fields = _class_fields(node)[:6]
        methods = _class_methods(node)
        init_methods = [method for method in methods if method.name == "__init__"]
        public_methods = [method for method in methods if method.name != "__init__"]

        def add_method(method: ast.AST) -> None:
            rows.append((method.lineno, "    " + _signature(method)))

        # Schema classes are their fields. Behavioral classes lead with the method a caller hits.
        if len(fields) >= len(public_methods):
            if doc:
                rows.append(doc)
            rows.extend(fields)
            for method in init_methods + public_methods:
                add_method(method)
        else:
            if doc:
                rows.append(doc)
            for method in public_methods + init_methods:
                add_method(method)
            rows.extend(fields)
        symbol = node.name
        title = f"class {node.name}"
    else:
        rows.append((node.lineno, _signature(node)))
        doc = _doc_line(node)
        if doc:
            rows.append((doc[0], "    " + doc[1].strip()))
        symbol = node.name
        title = node.name
    rows = _cap_rows(rows, 16, 1200)
    return _card(kind, rel, title, rows, symbol=symbol)


def _class_methods(node: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    methods = []
    for child in node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if child.name.startswith("_") and child.name != "__init__":
            continue
        if child.name.startswith("validate"):
            continue
        methods.append(child)
    return methods[:6]


def _class_fields(node: ast.ClassDef) -> list[tuple[int, str]]:
    fields = []
    for child in node.body:
        if not isinstance(child, ast.AnnAssign) or not isinstance(child.target, ast.Name):
            continue
        if child.target.id.startswith("_") or child.annotation is None:
            continue
        fields.append((child.lineno, f"    {child.target.id}: {ast.unparse(child.annotation)}"))
    return fields


def _signature(node: ast.AST) -> str:
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({ast.unparse(node.args)}){returns}:"


def _doc_line(node: ast.AST) -> tuple[int, str] | None:
    doc = ast.get_docstring(node)
    if not doc:
        return None
    paragraph = " ".join(doc.strip().split("\n\n", 1)[0].split())
    if len(paragraph) > 280:
        paragraph = paragraph[:277].rstrip() + "…"
    line_no = node.lineno
    if node.body and isinstance(node.body[0], ast.Expr):
        line_no = node.body[0].lineno
    return line_no, f'    """{paragraph}"""'


def _score(card: Evidence, readme: str, example_text: str, exported: set[str]) -> float:
    score = {
        "readme_section": 3.0,
        "quote": 2.0,
        "example": 3.0,
        "class": 3.0,
        "function": 2.5,
        "result": 5.0,
    }.get(card.kind, 0.0)
    title = card.title.lower()
    head = card.text[:400].lower()
    for word, weight in KEYWORD_WEIGHTS:
        if word in title:
            score += weight
        elif word in head:
            score += weight * 0.5
    if card.kind == "readme_section" and card.start_line <= 40:
        score += 2.0
    if card.symbol:
        if _mentioned(card.symbol, readme):
            score += 6.0
        if card.symbol in exported:
            score += 4.0
        if re.search(rf"(?<!\.)\b{re.escape(card.symbol)}\s*\(", example_text):
            score += 8.0
        elif re.search(rf"\b{re.escape(card.symbol)}\b", example_text):
            score += 3.0
        if card.symbol == "main" or card.symbol.startswith("_"):
            score -= 3.0
        if '"""' in card.text:
            score += 1.5
    if _is_root_readme(card.path) and card.kind in {"readme_section", "quote"}:
        score += 3.0
    if card.kind == "example" and re.search(r"^(?:import |from )\w", card.text, re.M):
        score += 5.0
    if card.kind == "example" and re.search(r"^\S+/\s{2,}\S", card.text, re.M):
        score -= 5.0
    if card.kind == "example" and re.search(r"\b(def |class |import |from )", card.text):
        score += 2.0
    if card.kind == "example" and re.search(r"quick\s*start|getting started|usage|how to use", card.title, re.I):
        score += 6.0
    if card.symbol == "main" or card.title == "main entry":
        score += 4.0
    if card.kind == "example" and re.search(r"@\w+\{", card.text):
        score -= 8.0
    if card.kind == "quote" and re.search(r"\b(avoid|without|instead|problem)\b", card.text, re.I):
        score += 3.0
    if "/test" in card.path or card.path.startswith("tests/"):
        score -= 4.0
    score -= 0.15 * card.path.count("/")
    return score


def _mentioned(symbol: str, readme: str) -> bool:
    if re.search(rf"\b{re.escape(symbol)}\s*\(", readme):
        return True
    if len(symbol) >= 5 and re.search(rf"\b{re.escape(symbol)}\b", readme):
        return True
    return False


def has_table(text: str) -> bool:
    return bool(re.search(r"^\|.*\|", text, re.M) and re.search(r"^\|?\s*:?-{3,}", text, re.M))


def _is_root_readme(path: str) -> bool:
    return "/" not in path and Path(path).name.lower().startswith("readme")


def _downrank_duplicate_methods(cards: list[Evidence]) -> None:
    methods: set[str] = set()
    for card in cards:
        if card.kind == "class":
            methods.update(re.findall(r"\bdef\s+(\w+)", card.text))
    for card in cards:
        if card.kind == "function" and card.symbol in methods:
            card.score -= 5.0


def _select(cards: list[Evidence], exported: set[str] | None = None, limit: int = 14) -> list[Evidence]:
    ranked = sorted(cards, key=lambda card: card.score, reverse=True)
    chosen: list[Evidence] = []
    seen: set[tuple] = set()

    def add(card: Evidence | None) -> None:
        if card is None or len(chosen) >= limit:
            return
        key = (card.path, card.start_line, card.kind, card.symbol or card.title)
        if key in seen:
            return
        seen.add(key)
        chosen.append(card)

    root_sections = [card for card in cards if card.kind == "readme_section" and _is_root_readme(card.path)]
    if root_sections:
        add(min(root_sections, key=lambda card: card.start_line))
    tables = [card for card in cards if has_table(card.text)]
    if tables:
        add(max(tables, key=lambda card: (card.text.count("|"), card.score)))
    example_text = "\n".join(card.text for card in cards if card.kind == "example")
    called = [
        card
        for card in ranked
        if card.symbol and re.search(rf"(?<!\.)\b{re.escape(card.symbol)}\s*\(", example_text)
    ]
    for card in called[:3]:
        add(card)
    for card in ranked:
        if card.symbol and exported and card.symbol in exported and card.kind in {"class", "function"}:
            add(card)
    import_examples = [
        card
        for card in cards
        if card.kind == "example" and not re.search(r"@\w+\{", card.text)
    ]
    if import_examples:
        best_example = max(import_examples, key=usage_rank)
        add(best_example)
        heading = best_example.title[: -len(" example")] if best_example.title.endswith(" example") else ""
        parent = next(
            (card for card in cards if card.path == best_example.path and card.title == heading and card.kind != "example"),
            None,
        )
        add(parent)

    for kind in ("readme_section", "quote", "result", "example", "class", "function"):
        matches = [card for card in ranked if card.kind == kind and card.score > 0]
        for card in matches[:2]:
            add(card)
    for card in ranked:
        if card.score < 1.5:
            continue
        add(card)
    selected = chosen[:limit]
    kind_order = {"readme_section": 0, "quote": 1, "result": 2, "example": 3, "class": 4, "function": 5}
    selected.sort(key=lambda card: (kind_order.get(card.kind, 9), card.path, card.start_line))
    for index, card in enumerate(selected, start=1):
        card.id = f"E{index}"
    return selected


def _exported_names(repo: Path, skip: set[Path]) -> set[str]:
    names: set[str] = set()
    for path in repo.rglob("__init__.py"):
        if _skipped(path, repo) or _under(path, skip):
            continue
        try:
            tree = ast.parse(_read(path))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
                continue
            if isinstance(node.value, (ast.List, ast.Tuple)):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        names.add(elt.value)
    return names


def _card(
    kind: str,
    path: str,
    title: str,
    rows: list[tuple[int, str]],
    symbol: str | None = None,
    end_line: int | None = None,
) -> Evidence:
    text = "\n".join(line for _, line in rows).strip()
    start = rows[0][0] if rows else 1
    end = end_line or (rows[-1][0] if rows else start)
    return Evidence(
        id="",
        kind=kind,
        path=path,
        start_line=start,
        end_line=end,
        title=title[:120],
        text=text,
        rows=rows,
        symbol=symbol,
    )


def _prose_rows(chunk: list[str], start_line: int) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    in_fence = False
    for offset, line in enumerate(chunk):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("<") or stripped.startswith("!["):
            continue
        rows.append((start_line + offset, line.rstrip()))
    while rows and not rows[0][1].strip():
        rows.pop(0)
    while rows and not rows[-1][1].strip():
        rows.pop()
    return rows


def _cap_rows(rows: list[tuple[int, str]], max_rows: int, max_chars: int) -> list[tuple[int, str]]:
    kept: list[tuple[int, str]] = []
    total = 0
    for line_no, line in rows:
        if len(kept) >= max_rows:
            break
        if total + len(line) > max_chars and kept:
            break
        kept.append((line_no, line))
        total += len(line) + 1
    return kept


def _section_kind(title: str, path: str) -> str:
    blob = f"{path} {title}".lower()
    if any(word in blob for word in ("result", "benchmark", "evaluation", "metric")):
        return "result"
    return "readme_section"


def _outside_fences(raw: str):
    ranges = [(match.start(), match.end()) for match in FENCE.finditer(raw)]

    def outside(pos: int) -> bool:
        return not any(start <= pos < end for start, end in ranges)

    return outside


def _line_of(raw: str, pos: int) -> int:
    return raw[:pos].count("\n") + 1


def _last_line(raw: str) -> int:
    if not raw:
        return 1
    if raw.endswith("\n"):
        return raw.count("\n")
    return raw.count("\n") + 1


SUMMARY = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.I | re.S)
SHELL_LANG = {"bash", "sh", "shell", "zsh"}


def _fence_groups(matches: list[re.Match], raw: str) -> list[list[re.Match]]:
    """Join nearby shell fences that are one pipeline split across two blocks."""
    groups: list[list[re.Match]] = []
    index = 0
    while index < len(matches):
        group = [matches[index]]
        if _fence_lang(matches[index]) in SHELL_LANG:
            nxt = index + 1
            while nxt < len(matches) and _fence_lang(matches[nxt]) in SHELL_LANG:
                gap = raw[group[-1].end() : matches[nxt].start()]
                same_heading = _heading_before(raw, group[0].start()) == _heading_before(raw, matches[nxt].start())
                if len(gap) > 400 or not same_heading:
                    break
                group.append(matches[nxt])
                nxt += 1
            index = nxt
        else:
            index += 1
        groups.append(group)
    return groups


def _fence_lang(match: re.Match) -> str:
    return match.group(1).strip().lower()


def _looks_like_commands(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return re.match(
            r"(?:from |import |bash |sh |python |pip |conda |export |[A-Z][A-Z0-9_]*=)",
            stripped,
        ) is not None
    return False


def _plain_heading(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split())


def _heading_before(raw: str, pos: int) -> str:
    headings = [(match.end(), match.group(2).strip()) for match in HEADING.finditer(raw[:pos])]
    headings.extend(
        (match.end(), _plain_heading(match.group(1)))
        for match in SUMMARY.finditer(raw[:pos])
        if _plain_heading(match.group(1))
    )
    if not headings:
        return ""
    return max(headings)[1]


def _skipped(path: Path, repo: Path) -> bool:
    return bool(set(path.relative_to(repo).parts) & SKIP_DIRS)


def _under(path: Path, roots: set[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")
