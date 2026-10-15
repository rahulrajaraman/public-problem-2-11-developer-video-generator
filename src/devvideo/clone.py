"""Resolve a GitHub repository or a local checkout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class CloneError(RuntimeError):
    pass


GITHUB_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_SHORT = re.compile(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


@dataclass
class RepoRef:
    path: Path
    slug: str
    origin: str
    cloned: bool


def parse_github(spec: str) -> tuple[str, str] | None:
    """Return (clone URL, slug) for a GitHub URL or owner/name."""
    text = spec.strip()
    match = GITHUB_URL.match(text) or GITHUB_SHORT.match(text)
    if not match:
        return None
    owner, name = match.group(1), match.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    return f"https://github.com/{owner}/{name}.git", f"{owner}-{name}"


def acquire(spec: str, out_dir: Path) -> RepoRef:
    """Use a local directory, or shallow-clone a GitHub repo into out_dir/source."""
    raw = spec.strip()
    local = Path(raw).expanduser()
    if local.exists():
        if not local.is_dir():
            raise CloneError(f"path is not a directory: {local}")
        resolved = local.resolve()
        return RepoRef(path=resolved, slug=_slug(resolved.name), origin=str(resolved), cloned=False)

    parsed = parse_github(raw)
    if parsed is None:
        raise CloneError(
            "expected a GitHub URL (https://github.com/owner/repo), owner/repo, or an existing directory"
        )
    url, slug = parsed
    dest = out_dir / "source"
    marker = dest / ".devvideo-origin"
    if dest.exists() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == url:
        return RepoRef(path=dest.resolve(), slug=slug, origin=url, cloned=True)
    if dest.exists():
        import shutil

        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CloneError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise CloneError(f"git clone failed for {url}: {detail}") from exc
    marker.write_text(url + "\n", encoding="utf-8")
    return RepoRef(path=dest.resolve(), slug=slug, origin=url, cloned=True)


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "repo"
