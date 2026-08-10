"""Prompt template loading and placeholder substitution.

Built-in templates ship as package data under ``rec/prompts/*.md``. User
templates in ``~/.config/rec/prompts/*.md`` override built-ins by filename stem
(``default.md`` in the user dir shadows the built-in ``default.md``).

Each template is a markdown file split by a line that is exactly ``---`` into a
**map** section and a **reduce** section. Both sections must contain their
respective placeholder (``{{transcript_chunk}}`` for map, ``{{map_output}}`` for
reduce) — a template missing either is a bug, caught at load time.

The loader returns a :class:`Template` with ``map_block`` and ``reduce_block``
ready for ``.format``-style substitution via :func:`fill`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config
from .log import get_logger

log = get_logger(__name__)

BUILTIN_NAMES = ("default", "standup", "client-call", "architecture-review", "interview")
_MAP_PLACEHOLDER = "{{transcript_chunk}}"
_REDUCE_PLACEHOLDER = "{{map_output}}"
# Split on a line that is exactly `---` (the section separator within a template).
_SECTION_SEP = re.compile(r"(?m)^---\s*$")


class TemplateError(Exception):
    """A template is malformed (missing sections or placeholders)."""


@dataclass(frozen=True)
class Template:
    """A loaded prompt template — a map block and a reduce block."""

    name: str
    map_block: str
    reduce_block: str

    def fill_map(self, transcript_chunk: str) -> str:
        return self.map_block.replace(_MAP_PLACEHOLDER, transcript_chunk)

    def fill_reduce(self, map_output: str) -> str:
        return self.reduce_block.replace(_REDUCE_PLACEHOLDER, map_output)


def _builtin_dir() -> Path:
    """The directory holding built-in templates (packaged with the module)."""
    return Path(__file__).resolve().parent / "prompts"


def _user_dir() -> Path:
    """The user template override directory (~/.config/rec/prompts)."""
    return config._config_home() / "prompts"


def list_template_names() -> list[str]:
    """All available template stems: built-ins plus user overrides (deduped)."""
    names: dict[str, None] = {}
    for n in BUILTIN_NAMES:
        names[n] = None
    udir = _user_dir()
    if udir.is_dir():
        for p in udir.glob("*.md"):
            names[p.stem] = None
    return list(names)


def load_template(name: str) -> Template:
    """Load a template by stem name.

    A user template at ``~/.config/rec/prompts/{name}.md`` shadows the built-in.
    Raises :class:`TemplateError` if the name is unknown or the template is
    malformed (missing a section or a placeholder).
    """
    user_path = _user_dir() / f"{name}.md"
    builtin_path = _builtin_dir() / f"{name}.md"

    if user_path.is_file():
        path = user_path
    elif builtin_path.is_file():
        path = builtin_path
    else:
        raise TemplateError(
            f"No template named {name!r}. Available: {', '.join(list_template_names())}."
        )

    raw = path.read_text(encoding="utf-8")
    log.info("loaded template %r from %s", name, path)
    return _parse(name, raw)


def load_template_file(path: str | Path) -> Template:
    """Load an arbitrary template file from disk (--template-file)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise TemplateError(f"Template file not found: {p}")
    raw = p.read_text(encoding="utf-8")
    log.info("loaded template file %s", p)
    return _parse(p.stem, raw)


def _parse(name: str, raw: str) -> Template:
    """Split a template into map/reduce sections and validate placeholders."""
    # The template has an optional system preamble before the first `---`,
    # then map and reduce sections. We treat the WHOLE file as split by `---`
    # into sections: take the section containing {{transcript_chunk}} as map,
    # and the section containing {{map_output}} as reduce.
    sections = [s.strip() for s in _SECTION_SEP.split(raw)]
    if len(sections) < 2:
        raise TemplateError(
            f"Template {name!r} has no `---` section separator — needs a map and a reduce section."
        )

    map_block = None
    reduce_block = None
    for section in sections:
        if _MAP_PLACEHOLDER in section and map_block is None:
            map_block = section
        elif _REDUCE_PLACEHOLDER in section and reduce_block is None:
            reduce_block = section

    if map_block is None:
        raise TemplateError(
            f"Template {name!r} is missing the {{transcript_chunk}} placeholder in its map section."
        )
    if reduce_block is None:
        raise TemplateError(
            f"Template {name!r} is missing the {{map_output}} placeholder in its reduce section."
        )

    return Template(name=name, map_block=map_block, reduce_block=reduce_block)
