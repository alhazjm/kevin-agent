"""Guard the slash-command bundles in hermes-config/skill-bundles/.

Why these tests exist (2026-09-04, hermes 0.20.6): the gateway now rejects
any slash-command it does not recognise ("Unknown command /log") instead of
forwarding it to the model, and a skill BUNDLE named `log` is what registers
`/log` again. Upstream's loader is deliberately forgiving — a bundle that
names a skill which does not exist still loads, with a silent "skipped"
note to the model — so a typo in `skills:` would ship as a slash command
that injects nothing. That is an M1-shaped failure (present in the repo,
absent in effect), and this file is the fail-loud guard for it.

The test venv has no PyYAML, and the bundle files are kept deliberately
flat so a strict line-based reader is enough here. If a bundle ever needs
richer YAML, add PyYAML to the documented test deps rather than loosening
this parser.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLES_DIR = REPO / "hermes-config" / "skill-bundles"
SKILLS_DIR = REPO / "skills"
DOCKERFILE = REPO / "Dockerfile"

# The four commands the runtime skills teach the user to type. If a fifth
# `/command` is added to a SKILL.md, add it here AND ship a bundle for it.
EXPECTED_COMMANDS = {"log", "undo", "budget", "summary"}

# Mirrors agent/skill_bundles.py::_slugify at the pinned SHA.
_INVALID = re.compile(r"[^a-z0-9-]")
_MULTI_HYPHEN = re.compile(r"-{2,}")


def _slugify(name: str) -> str:
    cmd = name.lower().replace(" ", "-").replace("_", "-")
    cmd = _INVALID.sub("", cmd)
    return _MULTI_HYPHEN.sub("-", cmd).strip("-")


def _read_bundle(path: Path) -> dict:
    """Strict flat reader: top-level scalars, one `skills:` list, and one
    `instruction: |` block. Anything else fails loudly."""
    out: dict = {"skills": [], "instruction": ""}
    mode = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("#") and mode != "instruction":
            continue
        if mode == "instruction":
            if raw.startswith("  ") or not raw.strip():
                out["instruction"] += raw[2:] + "\n"
                continue
            mode = None
        if mode == "skills":
            m = re.match(r"^  - (\S+)\s*$", raw)
            if m:
                out["skills"].append(m.group(1))
                continue
            mode = None
        if not raw.strip():
            continue
        assert not raw.startswith((" ", "\t")), f"{path.name}: unexpected indent: {raw!r}"
        key, _, value = raw.partition(":")
        value = value.strip()
        if key == "skills":
            assert value == "", f"{path.name}: skills must be a list"
            mode = "skills"
        elif key == "instruction":
            assert value == "|", f"{path.name}: instruction must be a `|` block"
            mode = "instruction"
        elif key in {"name", "description"}:
            out[key] = value.strip('"')
        else:
            raise AssertionError(f"{path.name}: unexpected key {key!r}")
    return out


def _skill_frontmatter_name(skill_dir: Path) -> str | None:
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return None
    for line in md.read_text(encoding="utf-8").splitlines()[:12]:
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


class TestBundleFiles:
    def test_every_expected_command_has_a_bundle(self):
        found = {p.stem for p in BUNDLES_DIR.glob("*.yaml")}
        assert found == EXPECTED_COMMANDS, f"bundles on disk {found} != expected {EXPECTED_COMMANDS}"

    def test_name_matches_filename_and_slugs_cleanly(self):
        for path in sorted(BUNDLES_DIR.glob("*.yaml")):
            b = _read_bundle(path)
            assert b.get("name"), f"{path.name}: missing name"
            assert _slugify(b["name"]) == path.stem, f"{path.name}: name {b['name']!r} slugs to {_slugify(b['name'])!r}"
            assert b.get("description"), f"{path.name}: missing description"
            assert b["instruction"].strip(), f"{path.name}: empty instruction"

    def test_every_listed_skill_exists_and_names_match(self):
        for path in sorted(BUNDLES_DIR.glob("*.yaml")):
            b = _read_bundle(path)
            assert b["skills"], f"{path.name}: empty skills list"
            for skill in b["skills"]:
                skill_dir = SKILLS_DIR / skill
                assert skill_dir.is_dir(), f"{path.name}: skill {skill!r} has no directory under skills/"
                fm = _skill_frontmatter_name(skill_dir)
                assert fm == skill, f"{path.name}: skills/{skill}/SKILL.md declares name {fm!r}, bundle says {skill!r}"

    def test_bundle_slug_does_not_shadow_a_runtime_skill(self):
        # A bundle and a skill with the same slug: the bundle silently wins.
        skill_slugs = {_slugify(p.name) for p in SKILLS_DIR.iterdir() if p.is_dir()}
        for path in BUNDLES_DIR.glob("*.yaml"):
            assert path.stem not in skill_slugs, f"{path.name} would shadow the {path.stem} skill"

    def test_log_bundle_restates_the_silence_contract(self):
        # CLAUDE.md invariant #1 must be stated at every call site.
        b = _read_bundle(BUNDLES_DIR / "log.yaml")
        assert "EMPTY" in b["instruction"] and "bubble_sent" in b["instruction"]
        assert 'source="manual"' in b["instruction"]


class TestBundlesShip:
    def test_dockerfile_copies_the_bundles_dir(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "COPY hermes-config/skill-bundles/ /root/.hermes/skill-bundles/" in text
