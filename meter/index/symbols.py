"""Symbol index: symbol name -> {file, start, end, kind, signature}.

Engine selection per meter-handoff.md Section 4/M2 is "tree-sitter if
available, else ctags, else a language-specific regex table." This
implementation covers the two ends that are actually tractable without
bundling per-language tree-sitter grammars/tag-queries this plugin doesn't
ship, and without a real ctags install to develop and verify against (this
dev environment has neither on PATH — see scripts/dag-doctor.py's external
tools check):

- **Python** uses the stdlib `ast` module — exact, no guessing. Section 5.3
  names `ast` specifically as one of the reasons no third-party parser is
  needed here.
- **Everything else** (JS/TS/Go/Java/Kotlin/PHP/Ruby) uses a regex table with
  a per-language block-end heuristic (brace counting, or keyword/`end`
  counting for Ruby). This is the documented "regex table" fallback tier, and
  for non-Python files it is currently the ONLY tier implemented — see the
  module docstring in meter/index/__init__.py. `VERIFY` and add tree-sitter/
  ctags once one is confirmed available in a real install to test against.

Every function is pure and never raises on malformed input: a file that fails
to parse contributes zero symbols, never an exception that could abort the
whole index (Rule 1, fail-open everywhere).
"""

from __future__ import annotations

import ast
import re

_BRACE_LANG_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".java",
                     ".kt", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".rs", ".swift"}
_RUBY_EXTS = {".rb"}

# One entry per (extensions, [(compiled pattern, kind), ...]). Patterns are
# matched against each line independently — this is a lexical heuristic, not a
# parser, so it can both miss unusual styles and (rarely) false-positive on a
# string that happens to look like a declaration. Both failure modes are
# acceptable for a context-budgeting index; neither is acceptable for
# anything that changes what code runs, which is exactly why this index never
# feeds anything but the dossier.
_REGEX_TABLE: list[tuple[set[str], list[tuple[re.Pattern, str]]]] = [
    ({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}, [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s+(\w+)\s*\("), "function"),
        (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*(?::\s*[\w<>\[\],\s|]+)?=>"), "function"),
        (re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*(\w+)\s*\([^)]*\)\s*(?::\s*[\w<>\[\],\s|]+)?\{"), "method"),
    ]),
    ({".go"}, [
        (re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("), "function"),
        (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)\b"), "class"),
    ]),
    ({".java", ".kt"}, [
        (re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)*(?:class|interface)\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:public|private|protected|static|final|\s)+[\w<>\[\],\s]+?\s(\w+)\s*\([^)]*\)\s*\{"), "method"),
    ]),
    ({".php"}, [
        (re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+(\w+)\s*\("), "function"),
    ]),
    ({".c", ".h", ".cpp", ".hpp"}, [
        (re.compile(r"^\s*(?:class|struct)\s+(\w+)"), "class"),
        (re.compile(r"^[\w:*&<>,\s]+?\s(\w+)\s*\([^;]*\)\s*\{"), "function"),
    ]),
    ({".cs"}, [
        (re.compile(r"^\s*(?:public|private|protected|internal|static|\s)*(?:class|interface)\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:public|private|protected|internal|static|\s)+[\w<>\[\],\s]+?\s(\w+)\s*\([^)]*\)\s*\{"), "method"),
    ]),
    ({".rs"}, [
        (re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)\s*[<(]"), "function"),
        (re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)"), "class"),
    ]),
    ({".swift"}, [
        (re.compile(r"^\s*(?:public|private|internal|\s)*(?:class|struct)\s+(\w+)"), "class"),
        (re.compile(r"^\s*(?:public|private|internal|\s)*func\s+(\w+)\s*\("), "function"),
    ]),
]

_RUBY_PATTERNS = [
    (re.compile(r"^\s*class\s+(\w+)"), "class"),
    (re.compile(r"^\s*module\s+(\w+)"), "class"),
    (re.compile(r"^\s*def\s+(?:self\.)?(\w+)"), "function"),
]
_RUBY_OPENERS = re.compile(r"^\s*(def|class|module|do\b|if\b|unless\b|case\b|while\b|until\b|begin\b)")
_RUBY_CLOSER = re.compile(r"^\s*end\b")

# Runaway guard: a mis-detected block (e.g. an unterminated string tripping the
# brace counter) must never scan the rest of a huge file looking for a close.
_MAX_BLOCK_LINES = 400


def index_file(rel_path: str, text: str) -> list[dict]:
    """Returns symbol records for one file's text. `rel_path` is stored
    verbatim as the record's `file` field — callers own path normalisation."""
    ext = _ext(rel_path)
    if ext == ".py":
        return _python_symbols(rel_path, text)
    if ext in _RUBY_EXTS:
        return _ruby_symbols(rel_path, text)
    if ext in _BRACE_LANG_EXTS:
        return _regex_symbols(rel_path, text, ext)
    return []


def _ext(rel_path: str) -> str:
    dot = rel_path.rfind(".")
    return rel_path[dot:].lower() if dot != -1 else ""


def _python_symbols(rel_path: str, text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []

    lines = text.splitlines()
    symbols: list[dict] = []
    stack: list[str] = []

    def signature(lineno: int) -> str:
        return lines[lineno - 1].strip() if 1 <= lineno <= len(lines) else ""

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualified = ".".join([*stack, node.name])
            end = getattr(node, "end_lineno", node.lineno)
            symbols.append({"name": qualified, "file": rel_path, "start": node.lineno,
                             "end": end, "kind": "method" if stack else "function",
                             "signature": signature(node.lineno)})
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()
        elif isinstance(node, ast.ClassDef):
            qualified = ".".join([*stack, node.name])
            end = getattr(node, "end_lineno", node.lineno)
            symbols.append({"name": qualified, "file": rel_path, "start": node.lineno,
                             "end": end, "kind": "class", "signature": signature(node.lineno)})
            stack.append(node.name)
            for child in ast.iter_child_nodes(node):
                visit(child)
            stack.pop()
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    visit(tree)
    return symbols


def _regex_symbols(rel_path: str, text: str, ext: str) -> list[dict]:
    patterns = None
    for exts, pats in _REGEX_TABLE:
        if ext in exts:
            patterns = pats
            break
    if not patterns:
        return []

    lines = text.splitlines()
    symbols: list[dict] = []
    for i, line in enumerate(lines):
        for pattern, kind in patterns:
            m = pattern.match(line)
            if not m:
                continue
            end = _brace_block_end(lines, i)
            symbols.append({"name": m.group(1), "file": rel_path, "start": i + 1,
                             "end": end, "kind": kind, "signature": line.strip()})
            break
    return symbols


def _brace_block_end(lines: list[str], start_idx: int) -> int:
    depth = 0
    seen_open = False
    limit = min(len(lines), start_idx + _MAX_BLOCK_LINES)
    for i in range(start_idx, limit):
        depth += lines[i].count("{") - lines[i].count("}")
        if "{" in lines[i]:
            seen_open = True
        if seen_open and depth <= 0:
            return i + 1
    return limit


def _ruby_symbols(rel_path: str, text: str) -> list[dict]:
    lines = text.splitlines()
    symbols: list[dict] = []
    for i, line in enumerate(lines):
        for pattern, kind in _RUBY_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            end = _ruby_block_end(lines, i)
            symbols.append({"name": m.group(1), "file": rel_path, "start": i + 1,
                             "end": end, "kind": kind, "signature": line.strip()})
            break
    return symbols


def _ruby_block_end(lines: list[str], start_idx: int) -> int:
    depth = 0
    limit = min(len(lines), start_idx + _MAX_BLOCK_LINES)
    for i in range(start_idx, limit):
        if _RUBY_OPENERS.match(lines[i]):
            depth += 1
        elif _RUBY_CLOSER.match(lines[i]):
            depth -= 1
            if depth <= 0:
                return i + 1
    return limit
