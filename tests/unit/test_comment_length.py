"""Comments and docstrings stay short and describe the code as it is: at most
2 lines (module docstrings 4), and no internal decision or review identifiers."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRECTORIES = ("src", "tests", "scripts", "alembic")
SCANNED_TEXT_FILES = (
    "AGENTS.md",
    "README.md",
    ".env.example",
    "Makefile",
    "Dockerfile",
    "compose.yaml",
    "compose.override.yml",
    "pyproject.toml",
)
MAX_COMMENT_LINES = 2
MAX_MODULE_DOCSTRING_LINES = 4
DOCSTRING_OWNERS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# Planning-note identifiers point into files that are not part of the repository.
INTERNAL_IDENTIFIER = re.compile(r"\b(D-\d+|R\d+-\d+|S4B?-[ARM]\d+|V\d{2}|M\d{2}|Q\d+)\b|§\s?\d")
# Alembic's own template writes these header lines.
ALEMBIC_TEMPLATE_LINE = re.compile(r"^(Revision ID|Revises|Create Date):")


def python_files() -> list[Path]:
    return sorted(
        path for directory in SCANNED_DIRECTORIES for path in (ROOT / directory).rglob("*.py")
    )


def comment_tokens(source: str) -> list[tokenize.TokenInfo]:
    return [
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and token.line.lstrip().startswith("#")
    ]


def docstring_length(text: str, *, is_alembic_revision: bool) -> int:
    lines = text.strip().splitlines()
    if is_alembic_revision:
        lines = [line for line in lines[1:] if not ALEMBIC_TEMPLATE_LINE.match(line)]
    return len([line for line in lines if line.strip()] if is_alembic_revision else lines)


def docstrings(source: str) -> list[tuple[int, str, int]]:
    found: list[tuple[int, str, int]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Module):
            limit = MAX_MODULE_DOCSTRING_LINES
        elif isinstance(node, DOCSTRING_OWNERS):
            limit = MAX_COMMENT_LINES
        else:
            continue
        text = ast.get_docstring(node)
        if text:
            found.append((getattr(node, "lineno", 1), text, limit))
    return found


def long_comment_blocks(source: str) -> list[int]:
    starts: list[int] = []
    run_length, run_start, previous_line = 0, 0, -2
    for token in comment_tokens(source):
        line = token.start[0]
        run_length, run_start = (
            (run_length + 1, run_start) if line == previous_line + 1 else (1, line)
        )
        previous_line = line
        if run_length == MAX_COMMENT_LINES + 1:
            starts.append(run_start)
    return starts


def violations_in(source: str, relative: str) -> list[str]:
    is_revision = relative.startswith("alembic/versions/")
    found = [
        f"{relative}:{line}: comment longer than allowed" for line in long_comment_blocks(source)
    ]
    found += [
        f"{relative}:{token.start[0]}: internal identifier in a comment"
        for token in comment_tokens(source)
        if INTERNAL_IDENTIFIER.search(token.string)
    ]
    for line, text, limit in docstrings(source):
        if docstring_length(text, is_alembic_revision=is_revision and line == 1) > limit:
            found.append(f"{relative}:{line}: docstring longer than allowed")
        if INTERNAL_IDENTIFIER.search(text):
            found.append(f"{relative}:{line}: internal identifier in a docstring")
    return found


def text_violations_in(text: str, relative: str) -> list[str]:
    lines = text.splitlines()
    found = [
        f"{relative}:{number}: internal identifier"
        for number, line in enumerate(lines, start=1)
        if INTERNAL_IDENTIFIER.search(line)
    ]
    if relative.endswith(".md"):
        return found
    run_length = 0
    for number, line in enumerate(lines, start=1):
        run_length = run_length + 1 if line.lstrip().startswith("#") else 0
        if run_length == MAX_COMMENT_LINES + 1:
            found.append(f"{relative}:{number - MAX_COMMENT_LINES}: comment longer than allowed")
    return found


def test_comments_and_docstrings_follow_the_rule() -> None:
    violations = [
        violation
        for path in python_files()
        for violation in violations_in(
            path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
        )
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.repo
def test_docs_and_build_files_follow_the_rule() -> None:
    violations = [
        violation
        for name in SCANNED_TEXT_FILES
        for violation in text_violations_in((ROOT / name).read_text(encoding="utf-8"), name)
    ]
    assert not violations, "\n".join(violations)


def test_the_scan_sees_each_kind_of_violation() -> None:
    body = (
        "# one\n# two\n# three\n# see D-77\n\n\n"
        'def f():\n    """a\n    b\n    c"""\n\n\n'
        'def g():\n    """R2-5."""\n'
    )
    kinds = {violation.split(": ", 1)[1] for violation in violations_in(body, "sample.py")}
    assert kinds == {
        "comment longer than allowed",
        "internal identifier in a comment",
        "docstring longer than allowed",
        "internal identifier in a docstring",
    }


def test_a_revision_header_counts_only_its_prose() -> None:
    template = '"""0009_x\n\nOne.\nTwo.\n\nRevision ID: a\nRevises: b\nCreate Date: c\n"""\n'
    prose = '"""0009_x\n\nOne.\nTwo.\nThree.\nFour.\nFive.\n\nRevision ID: a\n"""\n'
    assert violations_in(template, "alembic/versions/x.py") == []
    assert violations_in(prose, "alembic/versions/x.py") == [
        "alembic/versions/x.py:1: docstring longer than allowed"
    ]
    assert violations_in('"""x\n\nsee D-5\n"""\n', "alembic/versions/x.py") == [
        "alembic/versions/x.py:1: internal identifier in a docstring"
    ]


def test_text_files_are_scanned_line_by_line() -> None:
    assert text_violations_in("ok\nps: ## compose ps (V10)\n§2 note\n", "Makefile") == [
        "Makefile:2: internal identifier",
        "Makefile:3: internal identifier",
    ]
    assert text_violations_in("x\n# a\n# b\n# c\n", "Dockerfile") == [
        "Dockerfile:2: comment longer than allowed"
    ]
    assert text_violations_in("# a\n# b\n# c\n", "README.md") == []


def test_python_files_are_found() -> None:
    assert any(path.name == "test_comment_length.py" for path in python_files())


@pytest.mark.repo
def test_every_scanned_text_file_exists() -> None:
    assert all((ROOT / name).is_file() for name in SCANNED_TEXT_FILES)
