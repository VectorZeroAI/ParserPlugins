#!/usr/bin/env python3
"""
Ruby import resolver for ai-diagnos-lsp plugin system.

Handles:
    require_relative 'foo'          -> relative to current file
    require_relative '../bar/baz'   -> relative with traversal
    require 'foo'                   -> searched in lib/ and scope dirs
                                       (Bundler gem requires are skipped if not found)

Extension probing:
    exact, then .rb appended

Gemspec / Bundler gems (require 'rails', require 'json') are ignored when
they can't be found inside the project scope — those are external.
"""

import sys
import json
import re
from pathlib import Path

REQUIRE_RELATIVE_PATTERN = re.compile(
    r"""require_relative\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

REQUIRE_PATTERN = re.compile(
    r"""(?<![_\w])require\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)

STDLIB_NAMES = {
    # Commonly required stdlib names — extend as needed
    "json", "yaml", "csv", "date", "time", "set", "uri", "net/http",
    "net/https", "fileutils", "pathname", "logger", "digest", "base64",
    "cgi", "erb", "open-uri", "stringio", "tempfile", "timeout",
    "optparse", "pp", "pry", "benchmark", "English", "forwardable",
    "observer", "singleton", "thread", "monitor", "weakref",
    "rubygems", "bundler",
}


def probe_rb(base: Path) -> Path | None:
    if base.is_file():
        return base.resolve()
    rb = Path(str(base) + ".rb")
    if rb.is_file():
        return rb.resolve()
    return None


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for s in scope_dirs:
        try:
            path.relative_to(s)
            return True
        except ValueError:
            pass
    return False


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"ruby_parser: failed to parse stdin JSON: {e}\n")
        print("[]")
        return

    solid_file_content: str = payload.get("solid_file_content", "")
    project_scope: list[str] = payload.get("project_scope", [])
    file_path_str: str = payload.get("file_path", "")

    if not file_path_str:
        print("[]")
        return

    importing_file = Path(file_path_str).resolve()
    scope_dirs = [Path(s).resolve() for s in project_scope if Path(s).is_dir()]

    # Build search roots: scope dirs + common Ruby conventions (lib/, app/)
    search_roots: list[Path] = list(scope_dirs)
    for scope_dir in scope_dirs:
        for sub in ("lib", "app", "app/models", "app/controllers", "app/helpers"):
            candidate = scope_dir / sub
            if candidate.is_dir():
                search_roots.append(candidate.resolve())
    # Deduplicate
    search_roots = list(dict.fromkeys(search_roots))

    results: list[str] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if path not in seen:
            if not project_scope or is_within_scope(path, scope_dirs):
                seen.add(path)
                results.append(path.as_posix())

    # require_relative — always relative to current file
    for spec in REQUIRE_RELATIVE_PATTERN.findall(solid_file_content):
        base = (importing_file.parent / spec).resolve()
        resolved = probe_rb(base)
        if resolved:
            add(resolved)

    # require — search roots
    for spec in REQUIRE_PATTERN.findall(solid_file_content):
        # Skip known stdlib names
        if spec in STDLIB_NAMES or spec.startswith("rubygems"):
            continue
        for root in search_roots:
            base = (root / spec).resolve()
            resolved = probe_rb(base)
            if resolved:
                add(resolved)
                break  # found in this root, no need to keep searching

    print(json.dumps(results))


if __name__ == "__main__":
    main()

