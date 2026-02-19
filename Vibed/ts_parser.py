#!/usr/bin/env python3
"""
TypeScript import resolver for ai-diagnos-lsp plugin system.

Handles everything the JS parser handles, plus TypeScript-specific syntax:

  import type { Foo } from './foo'
  export type { Bar } from './bar'
  /// <reference path="./baz.d.ts" />
  /// <reference types="some-package" />

Resolution strategy (mirrors TypeScript's own module resolution — "Node16"
/ "Bundler" modes are the modern defaults):

  Relative imports:
    <path>          exact file
    <path>.ts
    <path>.tsx
    <path>.d.ts
    <path>/index.ts
    <path>/index.tsx
    <path>/index.d.ts
    — then fall back to JS extensions (for .js → .ts remapping)

  Path aliases (@/…, ~/…, etc.):
    Read tsconfig.json from the project root (first scope dir or file's
    ancestor) if present, extract "paths" and "baseUrl", and resolve
    accordingly. Falls back to treating the alias root as each scope dir.

  Bare specifiers inside scope:
    Same as JS parser — check each scope dir.

Note: .d.ts declaration files are included because they carry type
information that the LLM benefits from seeing during cross-file analysis.
"""

import sys
import json
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Patterns (superset of the JS patterns)
# ---------------------------------------------------------------------------

# static import/export … from "specifier"  (includes "import type")
STATIC_FROM_PATTERN = re.compile(
    r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]""",
    re.MULTILINE | re.DOTALL,
)

# side-effect import: import "specifier"
SIDE_EFFECT_PATTERN = re.compile(
    r"""import\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# dynamic import: import("specifier")
DYNAMIC_IMPORT_PATTERN = re.compile(
    r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# CommonJS require
REQUIRE_PATTERN = re.compile(
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# Triple-slash reference: /// <reference path="…" />
TRIPLE_SLASH_PATH_PATTERN = re.compile(
    r"""///\s*<reference\s+path\s*=\s*['"]([^'"]+)['"]\s*/?>""",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Extension probing — TypeScript-first, then JS fallbacks
# ---------------------------------------------------------------------------

TS_EXTENSIONS = [
    "",         # exact
    ".ts",
    ".tsx",
    ".d.ts",
    ".js",      # .js imports that map to .ts sources (TS "Rewrite to .js" mode)
    ".jsx",
    ".mts",
    ".cts",
    ".mjs",
    ".cjs",
]

TS_INDEX_NAMES = [
    "index.ts",
    "index.tsx",
    "index.d.ts",
    "index.js",
    "index.jsx",
    "index.mts",
    "index.mjs",
]


def probe_path(base: Path) -> Path | None:
    """
    Try to resolve `base` to an existing file using TypeScript extension
    probing rules.

    TypeScript allows writing `import './foo'` or `import './foo.js'` even
    when the actual source is `foo.ts` — we handle the .js→.ts remapping.
    """
    # 1. Exact file
    if base.is_file():
        return base.resolve()

    # 2. If the specifier ends in .js / .jsx / .mjs, TypeScript may actually
    #    mean the .ts / .tsx / .mts counterpart.
    ts_remap = {".js": ".ts", ".jsx": ".tsx", ".mjs": ".mts", ".cjs": ".cts"}
    if base.suffix in ts_remap:
        remapped = base.with_suffix(ts_remap[base.suffix])
        if remapped.is_file():
            return remapped.resolve()

    # 3. Append TypeScript / JS extensions
    for ext in TS_EXTENSIONS[1:]:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return candidate.resolve()

    # 4. Directory with index file
    if base.is_dir():
        for index_name in TS_INDEX_NAMES:
            candidate = base / index_name
            if candidate.is_file():
                return candidate.resolve()

    return None


# ---------------------------------------------------------------------------
# tsconfig path alias resolution
# ---------------------------------------------------------------------------

def load_tsconfig(scope_dirs: list[Path], file_path: Path) -> dict:
    """
    Attempt to load the nearest tsconfig.json.
    Searches upward from the importing file, then falls back to scope roots.
    """
    # Walk up from importing file
    for parent in [file_path.parent, *file_path.parents]:
        candidate = parent / "tsconfig.json"
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
                return json.loads(text), parent
            except Exception:
                pass

    # Try scope dirs
    for scope_dir in scope_dirs:
        candidate = scope_dir / "tsconfig.json"
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
                return json.loads(text), scope_dir
            except Exception:
                pass

    return {}, None


def build_path_aliases(tsconfig: dict, tsconfig_dir: Path) -> dict[str, list[Path]]:
    """
    Parse compilerOptions.paths and compilerOptions.baseUrl from tsconfig
    and return a dict mapping alias pattern → list of candidate base dirs.
    """
    aliases: dict[str, list[Path]] = {}
    compiler_options = tsconfig.get("compilerOptions", {})

    base_url_str = compiler_options.get("baseUrl")
    base_url = (tsconfig_dir / base_url_str).resolve() if base_url_str else tsconfig_dir

    paths = compiler_options.get("paths", {})
    for alias_pattern, mappings in paths.items():
        # alias_pattern may look like "@/*" or "~/*" or "@components/*"
        resolved_mappings: list[Path] = []
        for mapping in mappings:
            # Strip trailing /* wildcard — we do prefix replacement ourselves
            mapping_base = mapping.rstrip("*").rstrip("/")
            resolved_mappings.append((base_url / mapping_base).resolve())
        aliases[alias_pattern] = resolved_mappings

    return aliases, base_url


def apply_aliases(specifier: str, aliases: dict[str, list[Path]], base_url: Path | None) -> list[Path]:
    """
    Given a bare specifier, return candidate Path bases after alias expansion.
    Returns an empty list if no alias matched.
    """
    candidates: list[Path] = []

    for pattern, mapping_dirs in aliases.items():
        # Wildcard patterns like "@/*" → match "@/" prefix
        if pattern.endswith("/*"):
            prefix = pattern[:-1]  # "@/"
            if specifier.startswith(prefix):
                rest = specifier[len(prefix):]
                for mapping_dir in mapping_dirs:
                    candidates.append(mapping_dir / rest)
        elif pattern.endswith("*"):
            prefix = pattern[:-1]  # "@"
            if specifier.startswith(prefix):
                rest = specifier[len(prefix):]
                for mapping_dir in mapping_dirs:
                    candidates.append(mapping_dir / rest)
        else:
            # Exact alias
            if specifier == pattern:
                candidates.extend(mapping_dirs)

    # If baseUrl is set, bare specifiers resolve relative to it
    if base_url is not None and not candidates:
        candidates.append(base_url / specifier)

    return candidates


# ---------------------------------------------------------------------------
# Main resolution logic
# ---------------------------------------------------------------------------

def is_relative(specifier: str) -> bool:
    return specifier.startswith("./") or specifier.startswith("../")


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for scope_dir in scope_dirs:
        try:
            path.relative_to(scope_dir)
            return True
        except ValueError:
            pass
    return False


def resolve_specifier(
    specifier: str,
    importing_file: Path,
    scope_dirs: list[Path],
    aliases: dict,
    base_url: Path | None,
) -> Path | None:
    """Resolve a single import specifier."""

    # Ignore URLs, node built-ins, and absolute FS paths
    if (
        specifier.startswith("node:")
        or specifier.startswith("http://")
        or specifier.startswith("https://")
        or specifier.startswith("/")
    ):
        return None

    if is_relative(specifier):
        base = (importing_file.parent / specifier).resolve()
        return probe_path(base)

    # Non-relative: try aliases first, then scope dirs
    alias_candidates = apply_aliases(specifier, aliases, base_url)
    for candidate_base in alias_candidates:
        result = probe_path(candidate_base)
        if result is not None:
            return result

    # Bare specifier — try each scope dir
    for scope_dir in scope_dirs:
        base = (scope_dir / specifier).resolve()
        result = probe_path(base)
        if result is not None:
            return result

    return None


def extract_specifiers(source: str) -> list[str]:
    specifiers: list[str] = []
    specifiers.extend(STATIC_FROM_PATTERN.findall(source))
    specifiers.extend(SIDE_EFFECT_PATTERN.findall(source))
    specifiers.extend(DYNAMIC_IMPORT_PATTERN.findall(source))
    specifiers.extend(REQUIRE_PATTERN.findall(source))
    # Triple-slash references yield relative paths
    specifiers.extend(TRIPLE_SLASH_PATH_PATTERN.findall(source))
    return specifiers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"ts_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    if not file_path_str:
        print("[]")
        return

    importing_file = Path(file_path_str).resolve()
    scope_dirs: list[Path] = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    # Load tsconfig for path alias resolution
    tsconfig, tsconfig_dir = load_tsconfig(scope_dirs, importing_file)
    if tsconfig and tsconfig_dir:
        aliases, base_url = build_path_aliases(tsconfig, tsconfig_dir)
    else:
        aliases, base_url = {}, None

    specifiers = extract_specifiers(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for specifier in specifiers:
        resolved = resolve_specifier(specifier, importing_file, scope_dirs, aliases, base_url)
        if resolved is None or resolved in seen:
            continue
        if project_scope and not is_within_scope(resolved, scope_dirs):
            continue
        seen.add(resolved)
        results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

