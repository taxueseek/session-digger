"""CI gate: combo_map.json keys must reference real commands, skills, or scripts.

Prevents command/skill name drift — when a command is renamed or a skill is
removed, this test fails and prompts an update to combo_map.json.
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMBO_MAP = REPO_ROOT / "combo_map.json"
COMMANDS_DIR = REPO_ROOT / "commands"
SKILLS_DIR = REPO_ROOT / "skills"
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _collect_valid_keys() -> set:
    """Build the set of valid combo_map keys from current repo structure."""
    valid = set()

    # Slash commands: commands/*.md frontmatter `name:` field
    if COMMANDS_DIR.exists():
        for md in COMMANDS_DIR.glob("*.md"):
            text = md.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if name:
                        valid.add(name)
                    break

    # Skills: skills/<name>/SKILL.md frontmatter `name:` field
    if SKILLS_DIR.exists():
        for skill_dir in SKILLS_DIR.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                text = skill_md.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip().strip('"').strip("'")
                        if name:
                            valid.add(name)
                        break
                else:
                    # Fallback: use directory name if no frontmatter name
                    valid.add(skill_dir.name)

    # Scripts: scripts/<name>.py (flat files) + package dirs (echolib, index_builder)
    if SCRIPTS_DIR.exists():
        for f in SCRIPTS_DIR.iterdir():
            if f.is_file() and f.suffix in (".py", ".sh") and not f.name.startswith("_"):
                valid.add(f.name)
            elif f.is_dir() and (f / "__init__.py").exists() and not f.name.startswith("_"):
                valid.add(f.name)

    # Subskill scripts: skills/<name>/scripts/*.{py,sh} (e.g. env-doctor 探针脚本)
    if SKILLS_DIR.exists():
        for skill_dir in SKILLS_DIR.iterdir():
            sub_scripts = skill_dir / "scripts"
            if not sub_scripts.is_dir():
                continue
            for f in sub_scripts.iterdir():
                if f.is_file() and f.suffix in (".py", ".sh") and not f.name.startswith("_"):
                    valid.add(f.name)

    return valid


def test_combo_map_keys_are_valid():
    """Every combo_map combos key must map to a real command/skill/script."""
    if not COMBO_MAP.exists():
        pytest_skip("combo_map.json not found")
        return

    data = json.loads(COMBO_MAP.read_text(encoding="utf-8"))
    combos = data.get("combos", {})
    valid_keys = _collect_valid_keys()

    invalid = []
    for key in combos:
        # Normalize: "/recall" -> "recall", "/recall --decisions" -> "recall"
        base = key.split()[0] if " " in key else key
        if base.startswith("/"):
            base = base[1:]
        if base not in valid_keys:
            invalid.append(key)

    assert not invalid, (
        f"combo_map.json references {len(invalid)} missing command(s)/skill(s)/script(s): "
        f"{invalid[:5]}{'...' if len(invalid) > 5 else ''}. "
        f"Valid keys: {sorted(valid_keys)[:10]}..."
    )


def test_combo_map_values_are_valid():
    """Every combo_map combos value must also map to a real command/skill/script."""
    if not COMBO_MAP.exists():
        pytest_skip("combo_map.json not found")
        return

    data = json.loads(COMBO_MAP.read_text(encoding="utf-8"))
    combos = data.get("combos", {})
    valid_keys = _collect_valid_keys()

    invalid = []
    for _src, targets in combos.items():
        for tgt in targets:
            base = tgt.split()[0] if " " in tgt else tgt
            if base.startswith("/"):
                base = base[1:]
            if base not in valid_keys:
                invalid.append(tgt)

    assert not invalid, (
        f"combo_map.json targets {len(invalid)} missing command(s)/skill(s)/script(s): "
        f"{invalid[:5]}{'...' if len(invalid) > 5 else ''}"
    )


def pytest_skip(reason):
    """Skip helper that works with or without pytest."""
    try:
        import pytest
        pytest.skip(reason)
    except ImportError:
        print(f"SKIP: {reason}")


if __name__ == "__main__":
    # Standalone runner (no pytest dependency)
    try:
        test_combo_map_keys_are_valid()
        print("PASS: combo_map keys are valid")
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

    try:
        test_combo_map_values_are_valid()
        print("PASS: combo_map values are valid")
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

    print("ALL PASS")
