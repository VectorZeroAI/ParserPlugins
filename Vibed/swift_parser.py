#!/usr/bin/env python3
"""
Swift import resolver for ai-diagnos-lsp plugin system.

Swift's `import ModuleName` statement refers to compiled modules. For local
targets in a Swift Package Manager package we can resolve these to source
directories.

Strategy:
    1. Find Package.swift by walking up from the file.
    2. Parse target names and their source directories from Package.swift
       using simple regex (no Swift toolchain required).
    3. For each `import Foo` in the file, if "Foo" matches a local target
       name, return all .swift files in that target's sources directory.
    4. Additionally, handle `@testable import Foo` (same resolution).
    5. System / Apple framework imports (Foundation, UIKit, SwiftUI, etc.)
       are ignored.

Package.swift parsing is done with regex since it is a Swift DSL, not JSON.
We extract:
    .target(name: "Foo", ...)
    .executableTarget(name: "Foo", ...)
    .testTarget(name: "Foo", ...)
and infer source paths from the `path:` argument or fall back to Swift PM
conventions: Sources/<TargetName>/ or just the name itself.
"""

import sys
import json
import re
from pathlib import Path

# import Foo  /  @testable import Foo  /  import class Foo.Bar (module is Foo)
IMPORT_PATTERN = re.compile(
    r"""(?:@testable\s+)?import\s+(?:class|struct|enum|protocol|typealias|func|let|var\s+)?(\w+)""",
    re.MULTILINE,
)

# Target declarations in Package.swift
TARGET_PATTERN = re.compile(
    r"""\.(?:executable|test)?[Tt]arget\s*\(([^)]+)\)""",
    re.DOTALL,
)

NAME_IN_TARGET = re.compile(r"""name\s*:\s*["'](\w+)["']""")
PATH_IN_TARGET = re.compile(r"""path\s*:\s*["']([^"']+)["']""")

APPLE_FRAMEWORKS = {
    "Foundation", "UIKit", "AppKit", "SwiftUI", "Combine", "CoreData",
    "CoreGraphics", "CoreLocation", "CoreMotion", "CoreNFC", "CoreML",
    "ARKit", "RealityKit", "SceneKit", "SpriteKit", "GameKit", "StoreKit",
    "CloudKit", "WatchKit", "TVUIKit", "MessageUI", "MapKit", "EventKit",
    "HealthKit", "HomeKit", "CallKit", "UserNotifications", "Photos",
    "PhotosUI", "Contacts", "ContactsUI", "AuthenticationServices",
    "SafariServices", "WebKit", "AVFoundation", "AVKit", "Vision",
    "NaturalLanguage", "CreateML", "CoreImage", "CryptoKit", "Network",
    "NetworkExtension", "MultipeerConnectivity", "GameController",
    "MetalKit", "Metal", "ModelIO", "RealityFoundation", "Accessibility",
    "Swift", "Darwin", "Dispatch", "XCTest", "Testing", "OSLog",
}


def find_package_swift(start: Path) -> Path | None:
    for parent in [start.parent, *start.parents]:
        candidate = parent / "Package.swift"
        if candidate.is_file():
            return candidate
    return None


def parse_targets(package_swift: Path) -> dict[str, Path]:
    """
    Returns a dict mapping target name -> source directory.
    Falls back to SPM conventions if `path:` is not specified.
    """
    package_dir = package_swift.parent
    text = package_swift.read_text(encoding="utf-8", errors="ignore")
    targets: dict[str, Path] = {}

    for match in TARGET_PATTERN.finditer(text):
        body = match.group(1)
        name_m = NAME_IN_TARGET.search(body)
        if not name_m:
            continue
        name = name_m.group(1)

        path_m = PATH_IN_TARGET.search(body)
        if path_m:
            src_dir = (package_dir / path_m.group(1)).resolve()
        else:
            # SPM default: Sources/<TargetName>
            default = package_dir / "Sources" / name
            if default.is_dir():
                src_dir = default.resolve()
            else:
                # Fallback: root-level directory with target name
                fallback = package_dir / name
                src_dir = fallback.resolve() if fallback.is_dir() else default

        targets[name] = src_dir

    return targets


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
        sys.stderr.write(f"swift_parser: failed to parse stdin JSON: {e}\n")
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

    # Find and parse Package.swift
    package_swift = find_package_swift(importing_file)
    targets: dict[str, Path] = {}
    if package_swift:
        try:
            targets = parse_targets(package_swift)
        except Exception as e:
            sys.stderr.write(f"swift_parser: failed to parse Package.swift: {e}\n")

    imported_modules = IMPORT_PATTERN.findall(solid_file_content)

    results: list[str] = []
    seen: set[Path] = set()

    for module_name in imported_modules:
        if module_name in APPLE_FRAMEWORKS:
            continue

        src_dir = targets.get(module_name)
        if src_dir is None or not src_dir.is_dir():
            # Try searching scope dirs for a directory matching the module name
            for scope_dir in scope_dirs:
                candidate_dir = scope_dir / module_name
                if candidate_dir.is_dir():
                    src_dir = candidate_dir
                    break
                candidate_dir = scope_dir / "Sources" / module_name
                if candidate_dir.is_dir():
                    src_dir = candidate_dir
                    break

        if src_dir is None or not src_dir.is_dir():
            continue

        for swift_file in src_dir.rglob("*.swift"):
            resolved = swift_file.resolve()
            if resolved in seen:
                continue
            if project_scope and not is_within_scope(resolved, scope_dirs):
                continue
            seen.add(resolved)
            results.append(resolved.as_posix())

    print(json.dumps(results))


if __name__ == "__main__":
    main()

