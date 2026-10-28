#!/usr/bin/env python3
"""Link selected repository skills into a discovery directory without overwriting files."""

import argparse
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("skills", nargs="*", help="Skill directory names to link")
    parser.add_argument("--all", action="store_true", help="Link every skill in this repository")
    parser.add_argument("--dry-run", action="store_true", help="Preview without creating directories or links")
    parser.add_argument("--target", type=Path, default=Path.home() / ".agents" / "skills",
                        help="Discovery directory (default: ~/.agents/skills)")
    args = parser.parse_args()
    if args.all == bool(args.skills):
        parser.error("Specify skill names or --all, but not both")

    repository = Path(__file__).resolve().parents[1]
    available = {p.name: p.resolve() for p in (repository / "skills").iterdir()
                 if p.is_dir() and (p / "SKILL.md").is_file()}
    selected = sorted(available) if args.all else list(dict.fromkeys(args.skills))
    unknown = [name for name in selected if name not in available]
    if unknown:
        parser.error("Unknown skills: " + ", ".join(unknown))
    if not selected:
        parser.error("No skills found in this repository")

    target = args.target.expanduser().absolute()
    if target.exists() and not target.is_dir():
        parser.exit(1, f"Target is not a directory: {target}\n")
    plans = []
    conflicts = []
    for name in selected:
        source, destination = available[name], target / name
        if destination.is_symlink():
            try:
                matches = destination.resolve() == source
            except (OSError, RuntimeError):
                matches = False
            if matches:
                plans.append(("UNCHANGED", source, destination))
                continue
        if destination.exists() or destination.is_symlink():
            conflicts.append(str(destination))
        else:
            plans.append(("LINK", source, destination))
    if conflicts:
        parser.exit(1, "Existing paths conflict; no links created:\n" +
                    "\n".join(conflicts) + "\n")

    try:
        if not args.dry_run:
            target.mkdir(parents=True, exist_ok=True)
        for action, source, destination in plans:
            if action == "LINK" and not args.dry_run:
                # symlink refuses an existing destination, including one created after preflight.
                destination.symlink_to(source, target_is_directory=True)
            print(f"{'DRY-RUN ' if args.dry_run else ''}{action}: {destination} -> {source}")
    except OSError as exc:
        parser.exit(1, f"Linking stopped: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
