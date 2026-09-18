"""Import graph: 1-hop neighbours of a set of files (meter-handoff.md Section
4/M2). Implemented for Python (stdlib `ast`) and JS/TS (regex over
import/require statements) — the two ecosystems where "resolve a relative
import to a file that plausibly exists in this repo" is tractable without a
package manager's own resolution algorithm. PHP/Composer autoload resolution
(named in the spec) is NOT implemented: it needs to parse `composer.json`'s
autoload map, which is real work with its own edge cases, and shipping an
untested resolver would be worse than the dossier simply not having PHP's
1-hop neighbours yet. `neighbours_of` returns `[]` for anything it doesn't
recognise — never a guess.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?|export\s+(?:[\w*{}\s,]+\s+from\s+)?|require\()\s*['"]([^'"]+)['"]"""
)

_JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def neighbours_of(repo_root: Path, rel_path: str, text: str) -> list[str]:
    """Returns repo-relative paths of files `rel_path` imports, resolved
    against files that actually exist under `repo_root`. Best-effort: an
    import that doesn't resolve to a real file (an external package, a path
    alias this doesn't understand) is silently skipped rather than guessed."""
    ext = _ext(rel_path)
    if ext == ".py":
        return _python_neighbours(repo_root, rel_path, text)
    if ext in _JS_EXTS:
        return _js_neighbours(repo_root, rel_path, text)
    return []


def _ext(rel_path: str) -> str:
    dot = rel_path.rfind(".")
    return rel_path[dot:].lower() if dot != -1 else ""


def _python_neighbours(repo_root: Path, rel_path: str, text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []

    package_dir = Path(rel_path).parent
    modules: list[tuple[str, int]] = []  # (dotted module, relative level)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.append((alias.name, 0))
        elif isinstance(node, ast.ImportFrom):
            modules.append((node.module or "", node.level or 0))

    neighbours: list[str] = []
    for module, level in modules:
        candidate_dir = package_dir
        for _ in range(max(0, level - 1)):
            candidate_dir = candidate_dir.parent
        parts = module.split(".") if module else []
        candidate = candidate_dir.joinpath(*parts) if parts else candidate_dir
        resolved = _resolve_python_module(repo_root, candidate)
        if resolved is not None:
            neighbours.append(resolved)
    return sorted(set(neighbours))


def _resolve_python_module(repo_root: Path, candidate: Path) -> str | None:
    for suffix_path in (candidate.with_suffix(".py"), candidate / "__init__.py"):
        if (repo_root / suffix_path).is_file():
            return str(suffix_path).replace("\\", "/")
    return None


def _js_neighbours(repo_root: Path, rel_path: str, text: str) -> list[str]:
    package_dir = Path(rel_path).parent
    neighbours: list[str] = []
    for m in _JS_IMPORT_RE.finditer(text):
        spec = m.group(1)
        if not spec.startswith("."):
            continue  # a bare package specifier, not a repo-relative file
        resolved = _resolve_js_module(repo_root, package_dir / spec)
        if resolved is not None:
            neighbours.append(resolved)
    return sorted(set(neighbours))


def _resolve_js_module(repo_root: Path, candidate: Path) -> str | None:
    candidates = [candidate]
    candidates += [candidate.with_suffix(ext) for ext in _JS_EXTS]
    candidates += [candidate / f"index{ext}" for ext in _JS_EXTS]
    for c in candidates:
        try:
            normalized = Path(*[p for p in c.parts if p != "."])
        except ValueError:
            continue
        if (repo_root / normalized).is_file():
            return str(normalized).replace("\\", "/")
    return None
