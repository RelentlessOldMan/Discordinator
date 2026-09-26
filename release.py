#!/usr/bin/env python3
"""Cut a Discordinator release: bump the version, update CHANGELOG, commit, tag, push.

Usage:
  python release.py "one-line summary" [--bullet "text" ...] [--version X.Y.Z] [--no-push]

Defaults to bumping the patch number (1.0.x scheme). Assumes your actual code
changes are already committed — this creates the version-bump commit + a vX.Y.Z
tag on top, then pushes both (unless --no-push).

Examples:
  python release.py "Fix relay cursor off-by-one"
  python release.py "Add web transport" --version 1.1.0 --bullet "streamable-http" --bullet "config flag"
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INIT = ROOT / "src" / "discordinator" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def read_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"))
    if not m:
        sys.exit("could not find __version__ in __init__.py")
    return m.group(1)


def bump_patch(v: str) -> str:
    parts = v.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        sys.exit(f"version {v!r} is not X.Y.Z; pass --version explicitly")
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def set_version(new: str) -> None:
    text = INIT.read_text(encoding="utf-8")
    text = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new}"', text, count=1)
    INIT.write_text(text, encoding="utf-8")


def prepend_changelog(new: str, summary: str, bullets: list[str]) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    entry = f"## [{new}] - {date.today().isoformat()}\n{summary}\n"
    entry += "".join(f"- {b}\n" for b in bullets)
    entry += "\n"
    idx = text.find("\n## [")  # first existing version section
    if idx == -1:
        text = text.rstrip() + "\n\n" + entry
    else:
        insert_at = idx + 1
        text = text[:insert_at] + entry + text[insert_at:]
    CHANGELOG.write_text(text, encoding="utf-8")


def run(*args: str) -> None:
    print("+", " ".join(args))
    subprocess.run(args, cwd=str(ROOT), check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Cut a Discordinator release.")
    ap.add_argument("summary", help="one-line changelog summary / commit subject")
    ap.add_argument("--bullet", action="append", default=[], help="changelog bullet (repeatable)")
    ap.add_argument("--version", help="explicit version X.Y.Z (default: bump patch)")
    ap.add_argument("--no-push", action="store_true", help="commit + tag but do not push")
    args = ap.parse_args()

    current = read_version()
    new = args.version or bump_patch(current)
    print(f"releasing {current} -> {new}")

    set_version(new)
    prepend_changelog(new, args.summary, args.bullet)

    run("git", "add", str(INIT), str(CHANGELOG))
    run("git", "commit", "-m", f"v{new}: {args.summary}")
    run("git", "tag", f"v{new}")
    if not args.no_push:
        run("git", "push", "origin", "HEAD")
        run("git", "push", "origin", f"v{new}")
    print(f"done: v{new}")


if __name__ == "__main__":
    main()
