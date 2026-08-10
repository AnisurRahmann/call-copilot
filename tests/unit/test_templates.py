"""Tests for rec.templates — prompt template loading + placeholder validation.

Every built-in template must load by name and contain BOTH placeholders
(``{{transcript_chunk}}`` for map, ``{{map_output}}`` for reduce). User
templates override built-ins by filename stem.
"""

from __future__ import annotations

from rec import templates


def test_every_builtin_loads_by_name():
    """A missing template on a clean install is a shipping bug."""
    for name in templates.BUILTIN_NAMES:
        t = templates.load_template(name)
        assert t.name == name


def test_every_builtin_has_both_placeholders():
    """Each template needs a map AND a reduce section with its placeholder."""
    for name in templates.BUILTIN_NAMES:
        t = templates.load_template(name)
        assert "{{transcript_chunk}}" in t.map_block, f"{name} missing map placeholder"
        assert "{{map_output}}" in t.reduce_block, f"{name} missing reduce placeholder"


def test_fill_map_substitutes_chunk():
    t = templates.load_template("default")
    filled = t.fill_map("HELLO CHUNK")
    assert "HELLO CHUNK" in filled
    assert "{{transcript_chunk}}" not in filled


def test_fill_reduce_substitutes_map_output():
    t = templates.load_template("default")
    filled = t.fill_reduce("HELLO MAP")
    assert "HELLO MAP" in filled
    assert "{{map_output}}" not in filled


def test_unknown_template_errors():
    try:
        templates.load_template("does-not-exist")
        assert False, "expected TemplateError"
    except templates.TemplateError as e:
        assert "does-not-exist" in str(e)


def test_load_template_file(tmp_path):
    """--template-file loads an arbitrary .md from disk."""
    p = tmp_path / "custom.md"
    p.write_text(
        "System preamble.\n\n---\n\n"
        "Map section with {{transcript_chunk}}.\n\n"
        "---\n\n"
        "Reduce section with {{map_output}}.\n",
        encoding="utf-8",
    )
    t = templates.load_template_file(p)
    assert t.name == "custom"
    assert "{{transcript_chunk}}" in t.map_block
    assert "{{map_output}}" in t.reduce_block


def test_malformed_template_missing_placeholder_raises(tmp_path):
    p = tmp_path / "broken.md"
    p.write_text(
        "Map only with {{transcript_chunk}}.\n\n---\n\nReduce with no placeholder.\n",
        encoding="utf-8",
    )
    try:
        templates.load_template_file(p)
        assert False, "expected TemplateError for missing reduce placeholder"
    except templates.TemplateError as e:
        assert "map_output" in str(e)


def test_user_template_overrides_builtin(xdg):
    """A user template at ~/.config/rec/prompts/{name}.md shadows the built-in."""
    # The xdg fixture redirects XDG_CONFIG_HOME to tmp_path/config.
    user_dir = xdg / "config" / "rec" / "prompts"
    user_dir.mkdir(parents=True)
    (user_dir / "default.md").write_text(
        "map USER MARKER {{transcript_chunk}}\n\n---\n\nreduce {{map_output}}\n",
        encoding="utf-8",
    )
    t = templates.load_template("default")
    assert "USER MARKER" in t.map_block
