"""CI gate: every release manifest must carry the version SKILL.md declares.

One release has six version declarations in it, and they drifted silently:
``SKILL.md``, ``combo_map.json`` and the README badge were bumped to 0.9.25
while ``.claude-plugin/plugin.json`` still said 0.9.3, ``marketplace.json``
0.9.2, and ``herdr-plugin.toml`` 0.9.12. Nothing read them at build time, so
nothing noticed — an installer that keys update detection on the manifest
version simply never saw the last twenty releases.

The gate is written against ``SKILL.md``'s frontmatter as the single source of
truth rather than against a hard-coded number, because a hard-coded expectation
is exactly the thing that goes stale. Bumping one file and forgetting the rest
now fails here instead of shipping.
"""
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = REPO_ROOT / "SKILL.md"
COMBO_MAP = REPO_ROOT / "combo_map.json"
PLUGIN_JSON = REPO_ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
HERDR_TOML = REPO_ROOT / "herdr-plugin.toml"
README = REPO_ROOT / "README.md"
SKILLS_DIR = REPO_ROOT / "skills"


def _canonical_version() -> str:
    """The release version, from SKILL.md's YAML frontmatter."""
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"^version:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    assert match, "SKILL.md frontmatter carries no `version:` line"
    return match.group(1)


def _declared_versions() -> dict:
    """Every manifest's version, keyed by a human-readable label."""
    out = {}
    out[str(COMBO_MAP.relative_to(REPO_ROOT))] = json.loads(
        COMBO_MAP.read_text(encoding="utf-8")).get("version")
    out[str(PLUGIN_JSON.relative_to(REPO_ROOT))] = json.loads(
        PLUGIN_JSON.read_text(encoding="utf-8")).get("version")
    marketplace = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    for entry in marketplace.get("plugins", []):
        out[f"{MARKETPLACE_JSON.relative_to(REPO_ROOT)}#{entry.get('name')}"] = entry.get("version")
    toml_match = re.search(r'^version\s*=\s*"([^"]+)"',
                           HERDR_TOML.read_text(encoding="utf-8"), flags=re.MULTILINE)
    out[str(HERDR_TOML.relative_to(REPO_ROOT))] = toml_match.group(1) if toml_match else None
    # The README badge is a published declaration too: it is the first version a
    # visitor reads, and it was separately correct while the manifests were not.
    badge = re.search(r"badge/version-([0-9][^-]*)-", README.read_text(encoding="utf-8"))
    out["README.md badge"] = badge.group(1) if badge else None
    readme_text = README.read_text(encoding="utf-8")
    # The footer is a second README declaration. It sat at 0.9.19 for eight
    # releases while the badge said 0.9.27 — both are published text, so both
    # are declarations, and a visitor reading the bottom of the page learned a
    # different version than one reading the top.
    footer = re.search(r"session-digger\s+v([0-9][^\s<·]*)", readme_text)
    out["README.md footer"] = footer.group(1) if footer else None
    # herdr-plugin.toml carries a second version inside its description string:
    # "跨环境会话挖掘 v0.9.6：...". It is the text the installer shows the user,
    # and it was 21 releases behind while the `version =` line next to it was
    # correct — the one field a human reads was the one nothing checked.
    herdr_text = HERDR_TOML.read_text(encoding="utf-8")
    desc = re.search(r'v(\d+\.\d+(?:\.\d+)?)', herdr_text)
    out["herdr-plugin.toml description"] = desc.group(1) if desc else None
    return out


def _subskill_version_pairs() -> dict:
    """Each sub-skill's combo_map version against the SKILL.md beside it.

    Deliberately a separate function: a sub-skill versions independently of the
    main release, so these pairs must be *self*-consistent and must NOT be
    compared against the canonical release version. wechat-digger's combo_map
    said 0.0.8.1-pub against a SKILL.md at 0.0.9.3-pub — a sub-skill reporting
    its own version wrongly is indistinguishable from one never updated.
    """
    out = {}
    for skill_dir in sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir()):
        combo = skill_dir / "combo_map.json"
        skill_md = skill_dir / "SKILL.md"
        if not (combo.is_file() and skill_md.is_file()):
            continue
        sub = json.loads(combo.read_text(encoding="utf-8")).get("version")
        own = re.search(r"^version:\s*(\S+)\s*$",
                        skill_md.read_text(encoding="utf-8"), flags=re.MULTILINE)
        out[f"{combo.relative_to(REPO_ROOT)} vs {skill_md.relative_to(REPO_ROOT)}"] = (
            sub, own.group(1) if own else None)
    return out


def test_every_manifest_declares_one_version():
    canonical = _canonical_version()
    declared = _declared_versions()
    assert declared, "no release manifests were found to check"

    # Sub-skill entries are self-referential (combo_map vs its own SKILL.md) and
    # already carry their verdict; they must be equal, which the builder above
    # encodes as the plain version rather than an "a != b" string.
    mismatched = {label: ver for label, ver in declared.items() if ver != canonical}
    assert not mismatched, (
        f"release version is {canonical} (from SKILL.md), but these manifests "
        f"disagree: {mismatched}. Bump them together — an installer keying on "
        f"the manifest version cannot see a release it never had declared."
    )


def test_subskill_combo_maps_match_their_own_skill_md():
    """Sub-skills version independently, but not *inconsistently*."""
    bad = {label: pair for label, pair in _subskill_version_pairs().items()
           if pair[0] != pair[1]}
    assert not bad, (
        f"these sub-skill combo_map.json files disagree with the SKILL.md "
        f"beside them: {bad}")


def test_the_source_of_truth_is_actually_parsed():
    """Guard the guard: a regex that matches nothing would pass vacuously."""
    assert re.fullmatch(r"\d+(\.\d+)+", _canonical_version()), (
        "SKILL.md version must look like a dotted number; the gate reads it by regex")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
