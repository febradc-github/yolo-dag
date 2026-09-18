"""M2 Dossier builder (meter-handoff.md Section 4/M2).

Builds `.dag/runs/<run-id>/meter/dossiers/<node>.md` for one task node from
the run's own `tasks.json`, the symbol index and import graph (`meter/index/`),
a bounded text search, git history for the owned files, and any sibling
receipts (M1) that already touched the same files — zero model calls
anywhere, per the module's whole purpose: replace agent exploration with
deterministic precomputation.

Delivery (`SubagentStart`'s `additionalContext`) and enforcement (the
`PreToolUse` denial-with-escape-hatch on `Glob`/`Grep`) live in
meter/router.py; this module only builds the file and reports what it built.
Every I/O-touching step here degrades to "skip this section" rather than
raising — a dossier missing its git-log section is still useful; a crashed
builder blocking a spawn is not (Rule 1, fail-open everywhere).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import store
from . import attribution as attribution_mod
from . import intern as intern_mod
from . import pull as pull_mod
from .index import imports as imports_mod
from .index import symbols as symbols_mod
from .index import tokens as tokens_mod

_SECRET_NAME_RE = re.compile(
    r"(^|[./_-])(\.env(\..+)?|.*\.pem|.*\.key|id_rsa.*|.*\.p12|.*\.pfx|"
    r".*credentials.*|.*secrets?\.ya?ml)$",
    re.IGNORECASE,
)
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".tar", ".gz",
                 ".woff", ".woff2", ".ico", ".mp4", ".mov", ".exe", ".dll",
                 ".so", ".dylib", ".class", ".jar", ".bin"}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_MAX_FILE_BYTES = 200_000  # a file bigger than this isn't worth reading for a context pack
_MAX_SEARCH_TERMS = 8
_MAX_SEARCH_HITS_PER_TERM = 5
_MAX_GIT_LOG_ENTRIES = 5
_STDLIB_SEARCH_DEADLINE_S = 3.0  # this path only runs when `rg` is absent (see _search)


def is_secret_shaped(rel_path: str) -> bool:
    return bool(_SECRET_NAME_RE.search(Path(rel_path).name))


def is_binary_shaped(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in _BINARY_EXTS


def build(*, repo_root: Path, run_dir: Path, node_id: str, max_tokens: int,
          conn: Any = None, intern_cfg: dict | None = None,
          pull_cfg: dict | None = None) -> dict[str, Any]:
    """Builds and writes the dossier. Never raises. `conn` is optional: when
    given (a meter.store connection), the per-neighbour-file symbol spans
    found while building are also persisted via `store.set_dossier_spans`,
    which is what Clamp 3a's bounded reads (meter/clamp.py) look up later —
    without it, the dossier is still built and delivered, just without
    feeding 3a. `intern_cfg` is v2 M9's `meter.modules.intern` config dict;
    see meter/intern.py for the mode semantics and why `basename` (not
    `codebook`) is the default. `pull_cfg` is v2 M10's `meter.modules.pull`
    config dict — when `enabled`, sections are written individually and a
    manifest is returned instead of a single pushed body; see
    meter/pull.py's module docstring for why this defaults off."""
    try:
        return _build(repo_root=repo_root, run_dir=run_dir, node_id=node_id,
                       max_tokens=max_tokens, conn=conn, intern_cfg=intern_cfg or {},
                       pull_cfg=pull_cfg or {})
    except Exception:
        return {"node": node_id, "built": False, "path": None, "owned_files": [],
                "symbol_count": 0, "sibling_receipts": 0}


def _build(*, repo_root: Path, run_dir: Path, node_id: str, max_tokens: int,
           conn: Any = None, intern_cfg: dict, pull_cfg: dict) -> dict[str, Any]:
    task = load_task(run_dir, node_id)
    owns = [p for p in (task.get("owns") or []) if isinstance(p, str)] if task else []
    contract_ids = (task.get("contracts") or []) if task else []
    ac_list = (task.get("acceptance_criteria") or []) if task else []
    title = (task.get("title") or "") if task else ""

    safe_owns = [p for p in owns if not is_secret_shaped(p) and not is_binary_shaped(p)]
    file_texts = {p: t for p in safe_owns if (t := read_file(repo_root, p)) is not None}

    symbol_count = 0
    owned_symbols_by_file: dict[str, list[dict]] = {}
    neighbour_paths: set[str] = set()
    for rel_path, text in file_texts.items():
        syms = symbols_mod.index_file(rel_path, text)
        symbol_count += len(syms)
        owned_symbols_by_file[rel_path] = syms
        for n in imports_mod.neighbours_of(repo_root, rel_path, text):
            if n not in file_texts:
                neighbour_paths.add(n)

    neighbour_symbols_by_file: dict[str, list[dict]] = {}
    neighbour_spans: list[dict] = []
    for rel_path in sorted(neighbour_paths)[:20]:
        text = read_file(repo_root, rel_path)
        if text is None:
            continue
        syms = symbols_mod.index_file(rel_path, text)
        symbol_count += len(syms)
        neighbour_symbols_by_file[rel_path] = syms
        neighbour_spans.extend({"file": rel_path, "start": s["start"], "end": s["end"]}
                                for s in syms)

    if conn is not None:
        try:
            store.set_dossier_spans(conn, run_id=run_dir.name, node=node_id, spans=neighbour_spans)
        except Exception:
            pass  # 3a simply won't have span data for this node; the dossier itself still built

    # v2 M9 Intern: path labels for the render functions below. `path_label`
    # maps a real path to whatever should appear in the document body; the
    # header states the compression scheme once. See meter/intern.py.
    all_paths = sorted(set(owned_symbols_by_file) | set(neighbour_symbols_by_file))
    path_label, intern_header, codebook_dict = _intern_paths(all_paths, intern_cfg)

    owned_symbol_sections = [_render_symbols(p, syms, path_label(p))
                              for p, syms in owned_symbols_by_file.items()]
    neighbour_sections = [_render_symbols(p, syms, path_label(p))
                           for p, syms in neighbour_symbols_by_file.items()]

    contracts_text = load_contracts(run_dir, contract_ids)
    sibling_receipts = _sibling_receipts(run_dir, node_id, owns)
    git_log = _git_log(repo_root, owns)
    seed_terms = _seed_terms(title, owns, ac_list)
    search_hits = _search(repo_root, seed_terms)

    sections = _assemble_sections(
        node_id=node_id, title=title, owns=owns, ac_list=ac_list,
        contracts_text=contracts_text, owned_symbol_sections=owned_symbol_sections,
        neighbour_sections=neighbour_sections, sibling_receipts=sibling_receipts,
        git_log=git_log, search_hits=search_hits, intern_header=intern_header,
        path_label=path_label,
    )

    dossiers_dir = run_dir / "meter" / "dossiers"
    dossiers_dir.mkdir(parents=True, exist_ok=True)
    safe_node = node_id.replace("/", "_")

    # v2 M14 Attribution: what this dossier exposed the node to, beyond its
    # own owned files — see meter/attribution.py for why owned files aren't
    # tracked as exposures and what "referenced" means for the rest.
    exposures = attribution_mod.build_exposures(
        owns=owns, contracts_text=contracts_text,
        neighbour_paths=list(neighbour_symbols_by_file), sibling_receipts=sibling_receipts,
    )

    if pull_cfg.get("enabled"):
        # v2 M10 Pull (disabled by default — see meter/pull.py): sections go
        # to their own files, and a short manifest replaces the pushed body.
        manifest_sections = pull_mod.build_manifest_sections(sections)
        written = pull_mod.write_sections(run_dir, node_id, manifest_sections)
        manifest = pull_mod.render_manifest(node_id, written)
        # Also written to disk (not just returned) so a LATER, separate hook
        # request — SubagentStart's delivery, which doesn't share this
        # function's return value — can find it the same way it already
        # finds a push-mode dossier: by checking a deterministic path.
        manifest_path = dossiers_dir / f"{safe_node}" / "manifest.md"
        try:
            manifest_path.write_text(manifest, encoding="utf-8")
        except OSError:
            pass
        return {"node": node_id, "built": True, "path": None, "owned_files": owns,
                "symbol_count": symbol_count, "sibling_receipts": len(sibling_receipts),
                "tokens_est": tokens_mod.estimate_tokens(manifest), "exposures": exposures,
                "manifest": manifest, "pull_sections": written}

    body, used_tokens = _fit_to_budget(sections, max_tokens)
    path = dossiers_dir / f"{safe_node}.md"
    path.write_text(body, encoding="utf-8")

    if codebook_dict is not None:
        # v2 M9 Intern: "the codebook is stored per node so a later run can
        # decode an old receipt/dossier" — never required for the daemon's
        # own use (a dossier decodes nothing; it's read as-is by the agent
        # it was built for), only for a human or tool inspecting it later.
        try:
            (dossiers_dir / f"{safe_node}.codebook.json").write_text(
                json.dumps(codebook_dict, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            pass

    return {"node": node_id, "built": True, "path": str(path), "owned_files": owns,
            "symbol_count": symbol_count, "sibling_receipts": len(sibling_receipts),
            "tokens_est": used_tokens, "exposures": exposures}


def load_task(run_dir: Path, node_id: str) -> dict | None:
    tasks_path = run_dir / "tasks.json"
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for task in data.get("tasks", []):
        if task.get("id") == node_id:
            return task
    return None


def load_contracts(run_dir: Path, contract_ids: list) -> dict[str, str]:
    if not contract_ids:
        return {}
    tasks_path = run_dir / "tasks.json"
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    all_contracts = data.get("contracts", {})
    if isinstance(all_contracts, dict):
        return {cid: str(all_contracts[cid]) for cid in contract_ids if cid in all_contracts}
    if isinstance(all_contracts, list):
        by_id = {c.get("id"): c.get("text", "") for c in all_contracts if isinstance(c, dict)}
        return {cid: by_id[cid] for cid in contract_ids if cid in by_id}
    return {}


def read_file(repo_root: Path, rel_path: str) -> str | None:
    path = repo_root / rel_path
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _render_symbols(rel_path: str, syms: list[dict], path_label: str) -> str:
    """`path_label` is what's actually printed for this file — the real path,
    a basename-compressed suffix, or an Intern codebook code, depending on
    `meter.modules.intern.mode` (see `_intern_paths`)."""
    if not syms:
        return f"- `{path_label}` (no symbols indexed)"
    lines = [f"- `{path_label}`:"]
    for s in syms[:40]:
        lines.append(f"    - `{s['name']}` ({s['kind']}, {path_label}:{s['start']}-{s['end']}) "
                      f"`{s['signature']}`")
    return "\n".join(lines)


def _intern_paths(paths: list[str], intern_cfg: dict) -> tuple[Any, str | None, dict | None]:
    """Returns (path_label_fn, header_text_or_None) per `meter.modules.intern.mode`:

    - `off` (or unset/unrecognised): identity — every path prints in full.
    - `basename` (the default — see meter/intern.py's module docstring for
      why): strips the directory prefix shared by every file in this node,
      stated once in the header.
    - `codebook`: every file gets a short `f1`/`f2`/... code, with the
      file -> path table in the header. Not the default; requires an A/B
      harness to confirm no comprehension regression before it should be
      turned on for real work (see meter/intern.py)."""
    mode = intern_cfg.get("mode") if intern_cfg.get("enabled") else "off"

    if mode == "codebook":
        codebook = intern_mod.Codebook()
        label_map = {p: codebook.file_code(p) for p in paths}
        # The bare code is safe to use standalone from here on because the
        # table (below) always appears earlier in this same document — the
        # spec's semi-mnemonic mitigation ("references written as f1 only
        # after the table has appeared in the same prompt, never across a
        # message boundary where the table might be absent") is satisfied by
        # construction: a dossier is one self-contained document.
        header = codebook.render_table() if paths else None
        return (lambda p: label_map.get(p, p)), header, (codebook.to_dict() if paths else None)

    if mode == "basename" and len(paths) > 1:
        prefix, suffixes = intern_mod.basename_compress(paths)
        if prefix:
            header = f"Common path prefix for files below: `{prefix}`"
            return (lambda p: suffixes.get(p, p)), header, None

    return (lambda p: p), None, None


def _seed_terms(title: str, owns: list[str], ac_list: list) -> list[str]:
    text_parts = [title]
    for ac in ac_list:
        text_parts.append(ac if isinstance(ac, str) else str(ac.get("description", "")))
    for p in owns:
        text_parts.append(Path(p).stem)
    words = set()
    for part in text_parts:
        words.update(m.group(0) for m in _WORD_RE.finditer(part))
    # Longest/most-specific first — a distinctive symbol-shaped word is a
    # better search anchor than a common one.
    return sorted(words, key=len, reverse=True)[:_MAX_SEARCH_TERMS]


def _sibling_receipts(run_dir: Path, node_id: str, owns: list[str]) -> list[dict]:
    receipts_dir = run_dir / "meter" / "receipts"
    if not receipts_dir.is_dir():
        return []
    owns_set = set(owns)
    out = []
    for path in receipts_dir.glob("*.json"):
        if path.stem == node_id.replace("/", "_"):
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        receipt_files = {f.get("path") for f in (receipt.get("files") or []) if isinstance(f, dict)}
        if receipt_files & owns_set:
            out.append(receipt)
    return out


def _git_log(repo_root: Path, owns: list[str]) -> str | None:
    if not owns or not shutil.which("git"):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "log", f"-n{_MAX_GIT_LOG_ENTRIES}", "--oneline", "--",
             *owns],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _search(repo_root: Path, seed_terms: list[str]) -> list[str]:
    if not seed_terms:
        return []
    if shutil.which("rg"):
        return _search_ripgrep(repo_root, seed_terms)
    return _search_stdlib(repo_root, seed_terms)


def _search_ripgrep(repo_root: Path, seed_terms: list[str]) -> list[str]:
    # One process for every seed term at once (rg accepts repeated -e flags),
    # not one process per term — this runs inside PreToolUse's 10s deadline
    # (hooks/hooks.json), and _MAX_SEARCH_TERMS x a per-call subprocess
    # timeout could otherwise exceed it on its own.
    args = ["rg", "--line-number", "--max-count", str(_MAX_SEARCH_HITS_PER_TERM),
            "--fixed-strings"]
    for term in seed_terms:
        args += ["-e", term]
    args += ["--", str(repo_root)]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.splitlines()[:_MAX_SEARCH_TERMS * _MAX_SEARCH_HITS_PER_TERM]


def _search_stdlib(repo_root: Path, seed_terms: list[str]) -> list[str]:
    hits: list[str] = []
    scanned = 0
    deadline = time.monotonic() + _STDLIB_SEARCH_DEADLINE_S
    for path in repo_root.rglob("*"):
        if scanned > 2000 or len(hits) >= _MAX_SEARCH_TERMS * _MAX_SEARCH_HITS_PER_TERM:
            break
        if scanned % 100 == 0 and time.monotonic() > deadline:
            break  # this dev's repo is slow to walk without rg; return partial results, not none
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(repo_root))
        if is_secret_shaped(rel) or is_binary_shaped(rel):
            continue
        scanned += 1
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for term in seed_terms:
                if term in line:
                    hits.append(f"{rel}:{i}:{line.strip()}")
                    break
    return hits


def _assemble_sections(*, node_id: str, title: str, owns: list[str], ac_list: list,
                        contracts_text: dict[str, str], owned_symbol_sections: list[str],
                        neighbour_sections: list[str], sibling_receipts: list[dict],
                        git_log: str | None, search_hits: list[str],
                        intern_header: str | None = None,
                        path_label: Any = None) -> list[tuple[str, str]]:
    """Returns (label, markdown) pairs in priority order — highest priority
    first, so `_fit_to_budget` can cut from the tail."""
    sections: list[tuple[str, str]] = []
    label = path_label or (lambda p: p)

    header = [f"# Dossier: {node_id}", "",
              "A precomputed context pack, built from the repository index by deterministic "
              "tooling — no model call produced any part of this file."]
    if intern_header:
        header.append(f"\n{intern_header}")
    if title:
        header.append(f"\nTask: {title}")
    if owns:
        header.append(f"\nOwned files ({len(owns)}): " + ", ".join(f"`{label(p)}`" for p in owns))
    if ac_list:
        header.append("\nAcceptance criteria:")
        for ac in ac_list:
            header.append(f"- {ac if isinstance(ac, str) else ac.get('description', ac)}")
    sections.append(("header", "\n".join(header)))

    if contracts_text:
        lines = ["## Contracts"]
        for cid, text in contracts_text.items():
            lines.append(f"\n### {cid}\n\n{text}")
        sections.append(("contracts", "\n".join(lines)))

    if owned_symbol_sections:
        sections.append(("owned_symbols",
                          "## Symbols in owned files\n\n" + "\n".join(owned_symbol_sections)))

    if sibling_receipts:
        lines = [f"## Sibling receipts touching the same files ({len(sibling_receipts)})"]
        for r in sibling_receipts:
            lines.append(f"\n- `{r.get('node')}` — status: {r.get('status')}, "
                          f"files: {', '.join(f.get('path', '') for f in (r.get('files') or []))}")
        sections.append(("sibling_receipts", "\n".join(lines)))

    if neighbour_sections:
        sections.append(("neighbours",
                          "## Symbols in 1-hop import neighbours\n\n" + "\n".join(neighbour_sections)))

    if git_log:
        sections.append(("git_log", f"## Recent commits touching owned files\n\n```\n{git_log}\n```"))

    if search_hits:
        lines = ["## Text search hits for seed terms"]
        lines.extend(f"- `{h}`" for h in search_hits[:40])
        sections.append(("search", "\n".join(lines)))

    return sections


def _fit_to_budget(sections: list[tuple[str, str]], max_tokens: int) -> tuple[str, int]:
    kept: list[str] = []
    used = 0
    for _label, text in sections:
        cost = tokens_mod.estimate_tokens(text)
        if kept and used + cost > max_tokens:
            continue  # header (kept[0]) is never dropped; everything after is rankable
        kept.append(text)
        used += cost
    return "\n\n".join(kept) + "\n", used
