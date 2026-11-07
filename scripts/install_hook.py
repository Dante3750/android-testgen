#!/usr/bin/env python3
"""
install_hook.py - add a git pre-push check that lists changed classes with no tests.

Usage:
  python install_hook.py <project_root> [--block] [--uninstall]

By default the hook only WARNS (push continues) and suggests running `/android-testgen changed`.
With --block, the push is stopped until the changed classes have tests (bypass once with `git push --no-verify`).
It runs in about a second: it uses scan.py only, never Gradle.
"""
import os
import stat
import subprocess
import sys

MARK = "# android-testgen pre-push"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    root = os.path.abspath(argv[0])
    r = subprocess.run(["git", "-C", root, "rev-parse", "--git-path", "hooks"], capture_output=True, text=True)
    if r.returncode != 0:
        print("Not a git repository: %s" % root)
        return 2
    hooks = r.stdout.strip()
    hooks = hooks if os.path.isabs(hooks) else os.path.join(root, hooks)
    os.makedirs(hooks, exist_ok=True)
    hook = os.path.join(hooks, "pre-push")
    existing = open(hook, encoding="utf-8").read() if os.path.exists(hook) else ""

    if "--uninstall" in argv:
        if MARK not in existing:
            print("No android-testgen hook installed.")
            return 0
        kept = existing.split(MARK + " start")[0] + existing.split(MARK + " end")[-1].split("\n", 1)[-1]
        if kept.strip() in ("", "#!/bin/sh"):
            os.remove(hook)
        else:
            open(hook, "w", encoding="utf-8").write(kept)
        print("Removed android-testgen hook from %s" % hook)
        return 0
    if MARK in existing:
        print("Hook already installed in %s (use --uninstall first to change it)." % hook)
        return 0

    scan = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan.py").replace("\\", "/")
    block = "--block" in argv
    snippet = """%(mark)s start
PY=$(command -v python3 || command -v python)
if [ -n "$PY" ]; then
  REF=$(git rev-parse --abbrev-ref --symbolic-full-name @{u} 2>/dev/null || echo main)
  OUT=$("$PY" "%(scan)s" "$(git rev-parse --show-toplevel)" --changed-since "$REF" --fail-untested --top 15 2>&1)
  if [ $? -ne 0 ]; then
    echo "$OUT" | grep -E "^UNTESTED" >&2
    echo "Tip: run /android-testgen changed in Claude Code to add tests." >&2
    %(action)s
  fi
fi
%(mark)s end
""" % {"mark": MARK, "scan": scan,
       "action": "echo 'Push blocked (bypass once with: git push --no-verify).' >&2; exit 1" if block else ":"}
    content = (existing.rstrip("\n") + "\n\n" if existing else "#!/bin/sh\n") + snippet
    with open(hook, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("Installed %s pre-push check in %s" % ("blocking" if block else "warning", hook))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
