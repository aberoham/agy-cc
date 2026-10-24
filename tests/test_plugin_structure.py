"""Checks that the plugin can drop into thg-ai-plugins/plugins/agy unchanged.

Mirrors thg-ai-plugins' validate_plugins.py rules and its contributing
guidelines: no version fields, catalog fields matching the manifest, and
skills with a description and a Gotchas section.
"""

from __future__ import annotations

import json
import re

from conftest import REPO

PLUGIN = REPO / "plugins" / "agy"
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_and_catalog_follow_thg_rules():
    manifest = load(PLUGIN / ".claude-plugin" / "plugin.json")
    catalog = load(REPO / ".claude-plugin" / "marketplace.json")
    assert manifest["name"] == "agy"
    assert "version" not in manifest
    assert "version" not in catalog and "version" not in (catalog.get("metadata") or {})
    (entry,) = catalog["plugins"]
    assert "version" not in entry
    assert entry["source"] == "./plugins/agy"
    for field in ("description", "author"):
        assert entry[field] == manifest[field]


def test_every_skill_has_description_and_gotchas():
    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        front = FRONTMATTER.match(text)
        assert front and re.search(r"^description:[ \t]*\S", front[1], re.M), skill
        assert re.search(r"^## Gotchas", text, re.M), f"{skill} needs a Gotchas section"


def test_commands_have_frontmatter_and_use_the_companion():
    for command in sorted((PLUGIN / "commands").glob("*.md")):
        text = command.read_text(encoding="utf-8")
        assert FRONTMATTER.match(text), command
        if command.stem != "rescue":
            assert "agy_companion.py" in text, command
        assert "dangerously-skip-permissions" not in text


def test_plugin_carries_licence_and_notice():
    assert (PLUGIN / "LICENSE").read_text() == (REPO / "LICENSE").read_text()
    notice = (PLUGIN / "NOTICE").read_text()
    assert "codex-plugin-cc" in notice and "Modifications" in notice
