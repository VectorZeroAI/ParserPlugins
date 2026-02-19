#!/usr/bin/env python3
"""
Rust import resolver for ai-diagnos-lsp plugin system.

Rust's module system is file-based, declared with `mod` statements.

    mod foo;          -> looks for ./foo.rs  or  ./foo/mod.rs
    mod bar;          -> relative to the current file's directory
    pub mod baz;      -> same
    #[cfg(test)] mod tests;   -> same

`use` statements are NOT resolved here because they refer to items already
compiled into the crate — the source for those is found via `mod` declarations
in the module tree, not via `use` paths directly.

External crate imports (`use serde::...`) are also ignored since those live
in ~/.cargo and won't be in the project scope.

Strategy:
    1. Find Cargo.toml walking up from the file to identify the crate root.
    2. Parse `mod <name>;` declarations.
    3. For each declaration, probe:
         <file_dir>/<name>.rs
         <file_dir>/<name>/mod.rs
    4. Filter to scope and return.
"""

import sys
import json
import re
from pathlib import Path

# mod name; or pub mod name; (not `mod name { ... }` inline blocks)
MOD_DECL_PATTERN = re.compile(
    r"""^(?:pub\s+)?(?:#\[.*?\]\s*)?mod\s+(\w+)\s*;""",
    re.MULTILINE,
)


def find_cargo_toml(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        candidate = parent / "Cargo.toml"
        if candidate.is_file():
            return parent
    return None


def is_within_scope(path: Path, scope_dirs: list[Path]) -> bool:
    for s in scope_dirs:
        try:
            path.relative_to(s)
            return True
        except ValueError:
            pass
    return False


def resolve_mod(mod_name: str, importing_file: Path) -> Path | None:
    """
    Given a `mod foo;` declaration in `importing_file`, find the source file.

    Special case: if importing_file is main.rs / lib.rs / mod.rs,
    sibling modules live in the same directory.
    Otherwise they live next to the file or in a sub-directory named after
    the file stem (Rust 2018+ non-mod.rs submodule files).
    """
    file_dir = importing_file.parent
    stem = importing_file.stem

    candidates: list[Path] = []

    if importing_file.name in ("main.rs", "lib.rs", "mod.rs"):
        # Root or mod.rs — submodules are siblings
        candidates = [
            file_dir / f"{mod_name}.rs",
            file_dir / mod_name / "mod.rs",
        ]
    else:
        # Rust 2018 inline module file: foo.rs declares `mod bar;`
        # bar lives at foo/bar.rs or foo/bar/mod.rs
        candidates = [
            file_dir / stem / f"{mod_name}.rs",
            file_dir / stem / mod_name / "mod.rs",
            # Also try sibling for compatibility
            file_dir / f"{mod_name}.rs",
            file_dir / mod_name / "mod.rs",
        ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    return None


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.stderr.write(f"rust_parser: failed to parse stdin JSON: {e}\n")
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

    mod_names = MOD_DECL_PATTERN.findall(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for mod_name in mod_names:
        resolved = resolve_mod(mod_name, importing_file)
        if resolved is None or resolved in seen:
            continue
        if project_scope and not is_within_scope(resolved, scope_dirs):
            continue
        seen.add(resolved)
        results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

