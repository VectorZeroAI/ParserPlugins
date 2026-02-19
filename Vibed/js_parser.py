#!/usr/bin/env python3
"""
JavaScript import resolver for ai-diagnos-lsp plugin system.

Handles:
  ES Modules (static):
    import foo from './foo'
    import { bar } from '../bar'
    import * as baz from './baz'
    import './side-effect'
    export { x } from './x'
    export * from './y'

  ES Modules (dynamic):
    import('./foo')
    import( './foo' )

  CommonJS:
    require('./foo')
    const x = require('../bar')

Resolution strategy:
  - Relative paths (starting with . or ..) are resolved relative to the
    importing file.
  - Bare specifiers (no leading . / /) that exist as real files/dirs within
    the project scope are also resolved (i.e. local package-like imports).
  - node_modules and absolute-path imports are ignored.

Extension probing order (mirrors Node / bundler defaults):
    <path>          (exact)
    <path>.js
    <path>.jsx
    <path>/index.js
    <path>/index.jsx
    <path>.mjs
    <path>/index.mjs
"""

import sys
import json
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Static import / export … from "specifier"
STATIC_FROM_PATTERN = re.compile(
    r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]""",
    re.MULTILINE | re.DOTALL,
)

# Side-effect import: import "specifier"
SIDE_EFFECT_PATTERN = re.compile(
    r"""import\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

# Dynamic import: import("specifier") or import( "specifier" )
DYNAMIC_IMPORT_PATTERN = re.compile(
    r"""import\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# CommonJS require: require("specifier")
REQUIRE_PATTERN = re.compile(
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Extension probing
# ---------------------------------------------------------------------------

JS_EXTENSIONS = [
    "",          # exact match first
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
]

JS_INDEX_NAMES = [
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
]


def probe_path(base: Path) -> Path | None:
    """
    Try to resolve `base` to an existing file by probing JS-conventional
    extensions and index files.
    """
    # 1. Exact file
    if base.is_file():
        return base.resolve()

    # 2. With extension appended
    for ext in JS_EXTENSIONS[1:]:  # skip "" — already tried above
        candidate = base.with_suffix(ext) if base.suffix == "" else Path(str(base) + ext)
        # Avoid double extension: if base is already "foo.js", don't try "foo.js.js"
        if base.suffix and candidate == base:
            continue
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return candidate.resolve()

    # 3. As a directory with index file
    if base.is_dir():
        for index_name in JS_INDEX_NAMES:
            candidate = base / index_name
            if candidate.is_file():
                return candidate.resolve()

    return None


# ---------------------------------------------------------------------------
# Specifier resolution
# ---------------------------------------------------------------------------

def is_relative(specifier: str) -> bool:
    return specifier.startswith("./") or specifier.startswith("../")


def is_absolute_fs(specifier: str) -> bool:
    return specifier.startswith("/")


def resolve_specifier(specifier: str, importing_file: Path, scope_dirs: list[Path]) -> Path | None:
    """Resolve a single import specifier to an absolute Path, or None."""

    # Ignore node built-ins, URLs, and absolute FS paths
    if (
        specifier.startswith("node:")
        or specifier.startswith("http://")
        or specifier.startswith("https://")
        or is_absolute_fs(specifier)
    ):
        return None

    if is_relative(specifier):
        # Resolve relative to the directory of the importing file
        base = (importing_file.parent / specifier).resolve()
        return probe_path(base)
    else:
        # Bare specifier – check if it resolves to something inside the scope
        # (handles monorepo-local packages, path-mapped imports, etc.)
        for scope_dir in scope_dirs:
            base = (scope_dir / specifier).resolve()
            result = probe_path(base)
            if result is not None:
                return result

    return None


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for scope_dir in scope_dirs:
        try:
            path.relative_to(scope_dir)
            return True
        except ValueError:
            pass
    return False


def extract_specifiers(source: str) -> list[str]:
    specifiers: list[str] = []
    specifiers.extend(STATIC_FROM_PATTERN.findall(source))
    specifiers.extend(SIDE_EFFECT_PATTERN.findall(source))
    specifiers.extend(DYNAMIC_IMPORT_PATTERN.findall(source))
    specifiers.extend(REQUIRE_PATTERN.findall(source))
    return specifiers


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"js_parser: failed to parse stdin JSON: {e}\n")
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

    specifiers = extract_specifiers(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for specifier in specifiers:
        resolved = resolve_specifier(specifier, importing_file, scope_dirs)
        if resolved is None or resolved in seen:
            continue
        # Filter to scope
        if project_scope and not is_within_scope(resolved, scope_dirs):
            continue
        seen.add(resolved)
        results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

