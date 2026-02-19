#!/usr/bin/env python3
"""
Lua import resolver for ai-diagnos-lsp plugin system.

Handles Lua's require() system:
  - require("foo.bar")       -> foo/bar.lua or foo/bar/init.lua
  - require("foo/bar")       -> foo/bar.lua  (some frameworks use slash notation)
  - Resolves against each directory in project_scope.
"""

import sys
import json
import re
from pathlib import Path


# Matches: require("mod"), require('mod'), require "mod", require 'mod'
# Captures the module string.
REQUIRE_PATTERN = re.compile(
    r"""require\s*\(?\s*['"]([^'"]+)['"]\s*\)?""",
    re.MULTILINE,
)


def module_to_candidates(module_str: str) -> list[str]:
    """
    Given a Lua module string, return relative path candidates to check.
    Lua uses dots as separators (foo.bar -> foo/bar), but some code uses
    forward slashes directly. We handle both.
    """
    # Normalise: replace dots with slashes (ignore leading dots – Lua has no
    # relative-import dot syntax; dots are simply part of the name).
    normalised = module_str.replace(".", "/")

    return [
        f"{normalised}.lua",
        f"{normalised}/init.lua",
        # Also try the raw string as-is in case someone used slashes already
        # and we doubled-converted.
        f"{module_str}.lua",
        f"{module_str}/init.lua",
    ]


def resolve_module(module_str: str, scope_dirs: list[Path]) -> Path | None:
    """Try to find the file for a require() call within the given scope dirs."""
    candidates = module_to_candidates(module_str)

    for scope_dir in scope_dirs:
        for candidate in candidates:
            resolved = scope_dir / candidate
            if resolved.is_file():
                return resolved.resolve()

    return None


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for scope_dir in scope_dirs:
        try:
            path.relative_to(scope_dir)
            return True
        except ValueError:
            pass
    return False


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"lua_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    scope_dirs: list[Path] = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    # Also treat the directory containing the file itself as a candidate root
    # so that same-directory requires work even if the caller didn't list it
    # explicitly in scope.
    if file_path_str:
        file_dir = Path(file_path_str).resolve().parent
        if file_dir not in scope_dirs:
            scope_dirs.append(file_dir)

    found_modules = REQUIRE_PATTERN.findall(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for module_str in found_modules:
        resolved = resolve_module(module_str, scope_dirs)
        if resolved is not None and resolved not in seen:
            # Only return files that are within the declared project scope
            # (the file_dir fallback is allowed because the host system already
            # limits cross-file context to the scope it cares about, but we
            # respect the spirit of the protocol and only include scope files).
            if project_scope:
                project_scope_paths = [Path(s).resolve() for s in project_scope]
                if not is_within_scope(resolved, project_scope_paths):
                    continue
            seen.add(resolved)
            results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

