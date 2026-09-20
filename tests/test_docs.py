"""Guard the documentation a visitor actually sees.

Why these tests exist (2026-09-20): the README's sequence diagram shipped
with a semicolon inside a note ("LLM judgment;<br/>trip routing..."). In
Mermaid's sequence grammar a semicolon ends the statement, so GitHub showed
"Unable to render rich display" on the repo's front page instead of the
diagram. Nothing caught it, because nothing parsed the docs. The same day
the README went on a diet: its long sections moved into docs/, which is
exactly the kind of edit that leaves a dead relative link behind.

These are lint-level checks, not a Mermaid parser and not a link crawler.
They need no network and no extra dependency, in keeping with the rest of
the suite.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Directories that are not part of the published tree (local working
# folders, virtualenvs, caches). drafts/ and notes/ are gitignored.
_SKIP_DIRS = {".git", "node_modules", "drafts", "notes", "samples",
              ".pytest_cache", "__pycache__", ".venv", "venv", ".venv-test"}

_FENCE = re.compile(r"^(```|~~~)")
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_ENTITY = re.compile(r"#\w+;")            # Mermaid entity codes, e.g. #59;


def _markdown_files() -> list[Path]:
    out = []
    for path in REPO.rglob("*.md"):
        if _SKIP_DIRS.intersection(p.name for p in path.relative_to(REPO).parents):
            continue
        if path.relative_to(REPO).parts[0] in _SKIP_DIRS:
            continue
        out.append(path)
    return sorted(out)


def _split(text: str) -> tuple[list[tuple[int, str]], list[tuple[str, list[tuple[int, str]]]]]:
    """Return (prose_lines, fenced_blocks). Each line keeps its 1-indexed
    number; each block is (info_string, lines)."""
    prose, blocks = [], []
    current = None
    for n, line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        if _FENCE.match(line.strip()):
            if current is None:
                current = (line.strip().lstrip("`~").strip().lower(), [])
            else:
                blocks.append(current)
                current = None
            continue
        (current[1] if current is not None else prose).append((n, line))
    return prose, blocks


def _slug(heading: str) -> str:
    """GitHub's heading-anchor rule, near enough: drop markdown link
    targets and inline markup, lowercase, remove everything that is not a
    word character, space or hyphen, then spaces become hyphens."""
    heading = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    heading = re.sub(r"[`*_]", "", heading) if "`" in heading or "*" in heading else heading
    heading = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return heading.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    prose, _ = _split(path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    out = set()
    for _, line in prose:
        m = _HEADING.match(line)
        if not m:
            continue
        slug = _slug(m.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        out.add(slug if count == 0 else f"{slug}-{count}")
    return out


class TestMermaidBlocks:
    def test_sequence_diagram_text_has_no_statement_terminators(self):
        """`;` ends a statement and `#` starts an entity code in a Mermaid
        sequence diagram, so either one inside a message or a note breaks
        the whole block on GitHub. Write `#59;` if a semicolon is needed."""
        offenders = []
        for path in _markdown_files():
            _, blocks = _split(path.read_text(encoding="utf-8"))
            for info, lines in blocks:
                body = [l for l in lines if l[1].strip()]
                if info != "mermaid" or not body:
                    continue
                if body[0][1].strip() != "sequenceDiagram":
                    continue
                for n, line in body:
                    if ":" not in line:
                        continue
                    text = _ENTITY.sub("", line.split(":", 1)[1])
                    if ";" in text or "#" in text:
                        offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
        assert offenders == [], "Mermaid will not render:\n" + "\n".join(offenders)

    def test_the_purchase_flow_diagram_still_exists(self):
        """The guard above passes vacuously if the diagram is deleted, so
        pin that one sequence diagram lives somewhere in the docs."""
        found = [
            path for path in _markdown_files()
            if any(info == "mermaid" and any(l[1].strip() == "sequenceDiagram" for l in lines)
                   for info, lines in _split(path.read_text(encoding="utf-8"))[1])
        ]
        assert found, "no Mermaid sequence diagram left in any markdown file"


class TestLocalLinks:
    def test_relative_links_point_at_files_that_exist(self):
        missing = []
        for path in _markdown_files():
            prose, _ = _split(path.read_text(encoding="utf-8"))
            for n, line in prose:
                for target in _LINK.findall(re.sub(r"`[^`]*`", "", line)):
                    if re.match(r"^(https?:|mailto:|#)", target):
                        continue
                    file_part = target.split("#", 1)[0]
                    if not (path.parent / file_part).exists():
                        missing.append(f"{path.relative_to(REPO)}:{n}: {target}")
        assert missing == [], "dead relative links:\n" + "\n".join(missing)

    def test_heading_anchors_resolve(self):
        """`SETUP.md#swapping-the-pieces` style links, and same-file `#x`
        links, must name a heading that exists."""
        dead = []
        for path in _markdown_files():
            prose, _ = _split(path.read_text(encoding="utf-8"))
            for n, line in prose:
                for target in _LINK.findall(re.sub(r"`[^`]*`", "", line)):
                    if re.match(r"^(https?:|mailto:)", target) or "#" not in target:
                        continue
                    file_part, anchor = target.split("#", 1)
                    dest = (path.parent / file_part) if file_part else path
                    if dest.suffix.lower() != ".md" or not dest.exists():
                        continue
                    if anchor.lower() not in _anchors(dest):
                        dead.append(f"{path.relative_to(REPO)}:{n}: {target}")
        assert dead == [], "links to headings that do not exist:\n" + "\n".join(dead)


class TestReadmeIsTheFrontDoor:
    def test_readme_stays_short(self):
        """Design call, 2026-09-20: the README is the front door, not the
        manual. It went from 272 lines to under 100 by moving the build
        story and the architecture into docs/. If this fails, the new
        material belongs in docs/ with one line here pointing at it."""
        lines = (REPO / "README.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) <= 120, f"README.md is {len(lines)} lines; the ceiling is 120"

    def test_readme_points_an_agent_at_the_setup_walkthrough(self):
        """The README's copy-paste prompt names a section of AGENTS.md. If
        that heading is renamed, a cold-started agent is sent nowhere."""
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        heading = "Walking someone through first-time setup"
        assert heading in readme
        assert f"## {heading}" in agents
        assert heading in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
