"""Layering guard tests — pin the rule that the web layer is a thin consumer.

All business logic lives in the core modules (session, recorder, index,
transcriber, config, envcheck). The web layer reaches heavy dependencies
(audio capture, the whisper model, the sqlite index) ONLY through those core
modules, never by importing them directly. This test walks the AST of every
file under src/rec/web/ and fails if it finds a direct import of the
forbidden modules — so a future edit can't quietly pull audiotap or
faster_whisper into the browser server.

Also re-asserts the existing MCP-server read-only invariant (mcp_server
imports neither recorder nor web) so both guards live in one place.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "rec"

# Modules the web layer may use only via core wrappers. Direct imports would
# mean the browser server reaches into audio capture, the whisper model, or the
# sqlite index instead of going through recorder/transcriber/index.
_FORBIDDEN_IN_WEB = {"audiotap", "faster_whisper", "sqlite3"}


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.name]


def _imported_names(tree: ast.AST) -> set[str]:
    """Top-level module names imported by `tree` (first segment only)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split(".")[0])
    return names


WEB_DIR = SRC / "web"
WEB_FILES = _python_files(WEB_DIR)


@pytest.mark.parametrize("path", WEB_FILES, ids=[str(p.relative_to(SRC)) for p in WEB_FILES])
def test_web_module_imports_no_heavy_deps(path: Path) -> None:
    """No file under src/rec/web/ imports audiotap/faster_whisper/sqlite3 directly."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = _imported_names(tree) & _FORBIDDEN_IN_WEB
    assert not found, (
        f"{path.relative_to(SRC)} imports {found} directly — the web layer must "
        f"reach these via core modules (recorder/transcriber/index), not import them."
    )


def test_mcp_server_imports_neither_recorder_nor_web() -> None:
    """The read-only MCP server must not import the recording machinery or web UI."""
    src = (SRC / "mcp_server.py").read_text(encoding="utf-8")
    for forbidden in ("recorder", "transcriber", "audio_check", "web"):
        assert f"from . import {forbidden}" not in src
        assert f"import {forbidden}" not in src


def test_web_does_not_import_cli_directly_at_module_top() -> None:
    """The web layer reaches cli lazily (inside functions), not at module import.

    A top-level `from .. import cli` would create a heavy import cycle (cli
    pulls in recorder/transcriber/formatter). The endpoints that need cli
    (jobs transcription) import it inside the function body."""
    for path in WEB_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # Only top-level ImportFrom (level>=1, module == 'cli') is banned; lazy
        # imports live inside function bodies and are fine.
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "cli" in node.module:
                pytest.fail(
                    f"{path.relative_to(SRC)} imports cli at module top — do it "
                    f"lazily inside the function that needs it."
                )
