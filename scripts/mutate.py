#!/usr/bin/env python3
"""
mutate.py - mutation testing for one class, Kotlin or Java, with no Gradle plugin needed.

It makes small deliberate bugs ("mutants") in the production file, one at a time, re-runs the class's tests,
and reports which bugs the tests did NOT notice. A surviving mutant means a missing or weak assertion.

Usage:
  python mutate.py run <project_root> --file app/src/main/java/com/x/Foo.kt --module :app
                   --tests com.x.FooTest [--tests com.x.FooOtherTest] [--max 12] [--lines 20-80]
                   [--offline] [--timeout 600]
  python mutate.py list <project_root> --file <src>   # show the mutants it would try, change nothing
  python mutate.py restore <project_root>             # restore the source if a run was interrupted

Mutations (only in code, never inside strings or comments):
  ==  <-> !=        <  -> <=      >  -> >=     <= -> <     >= -> >
  &&  <-> ||        !x -> x       true <-> false
  +   <-> -         *  <-> /      %  -> *
  integer constant n -> n + 1 (limits, thresholds, caps)
  a standalone call statement is removed

Safety: the original file is backed up to .testgen/backup/ before the first mutant and restored after
every mutant, on Ctrl+C, and on errors. If the process is killed hard, run `mutate.py restore`.
It refuses to start if the tests don't pass on the unmodified code.

Results:  KILLED (a test failed - good), SURVIVED (no test noticed - needs a test),
          INVALID (mutant didn't compile - ignored), TIMEOUT (counted as killed).
Writes .testgen/mutation-<ClassName>.json. Exit code 0 if score >= --min-score (default 0), else 1.
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import coverage as cov  # noqa: E402

TOKEN_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*[\s\S]*?\*/|//[^\n]*')

OPERATORS = [
    # (name, regex on masked code, replacement)
    ("== -> !=", re.compile(r"(?<=\s)==(?=\s)"), "!="),
    ("!= -> ==", re.compile(r"(?<=\s)!=(?=\s)"), "=="),
    ("< -> <=", re.compile(r"(?<=\s)<(?=\s)"), "<="),
    ("> -> >=", re.compile(r"(?<=\s)>(?=\s)"), ">="),
    ("<= -> <", re.compile(r"(?<=\s)<=(?=\s)"), "<"),
    (">= -> >", re.compile(r"(?<=\s)>=(?=\s)"), ">"),
    ("&& -> ||", re.compile(r"&&"), "||"),
    ("|| -> &&", re.compile(r"\|\|"), "&&"),
    ("remove !", re.compile(r"(?<=[\s(])!(?=[A-Za-z_(])"), ""),
    ("true -> false", re.compile(r"\btrue\b"), "false"),
    ("false -> true", re.compile(r"\bfalse\b"), "true"),
    ("+ -> -", re.compile(r"(?<=\s)\+(?=\s)"), "-"),
    ("- -> +", re.compile(r"(?<=\s)-(?=\s)"), "+"),
    ("* -> /", re.compile(r"(?<=\s)\*(?=\s)"), "/"),
    ("/ -> *", re.compile(r"(?<=\s)/(?=\s)"), "*"),
    ("% -> *", re.compile(r"(?<=\s)%(?=\s)"), "*"),
]
# integer literals (not parts of names, floats or hex): n -> n + 1, which exposes untested limits and thresholds
CONST_RE = re.compile(r"(?<![\w.])(\d[\d_]*)([lL]?)(?![\w.])")
CALL_STMT_RE = re.compile(r"^\s*(?:this\.)?[a-z_][\w]*(?:\??\.[\w]+)*\??\.[a-z_]\w*\(.*\)\s*;?\s*$")
SKIP_LINE_RE = re.compile(r"^\s*(import|package|@|const\s|private\s+const|companion|\}|\{|$)")
KEYWORD_START = re.compile(r"^\s*(return|val|var|if|else|when|for|while|throw|try|catch|fun|class|object|"
                           r"super|this\(|emit|yield|assert|require|check)\b")


def mask(text):
    """Same-length copy of text where strings and comments are replaced by '#' (newlines kept)."""
    def repl(m):
        return re.sub(r"[^\n]", "#", m.group(0))
    return TOKEN_RE.sub(repl, text)


def line_starts(text):
    starts = [0]
    for i, c in enumerate(text):
        if c == "\n":
            starts.append(i + 1)
    return starts


def generate(text, lang, allowed_lines=None):
    masked = mask(text)
    starts = line_starts(text)
    lines = text.split("\n")
    mlines = masked.split("\n")
    mutants = []
    for idx, (ln, mln) in enumerate(zip(lines, mlines)):
        nr = idx + 1
        if allowed_lines is not None and nr not in allowed_lines:
            continue
        if SKIP_LINE_RE.match(mln) and not re.search(r"[=<>&|!+*/-]", mln.strip("{} ")):
            continue
        if re.match(r"^\s*(import|package)\b", mln) or re.match(r"^\s*@\w+(\(.*\))?\s*$", mln):
            continue
        for name, rx, rep in OPERATORS:
            for m in rx.finditer(mln):
                pos = starts[idx] + m.start()
                mutants.append({"line": nr, "op": name, "start": pos, "end": starts[idx] + m.end(), "rep": rep,
                                "code": ln.strip()[:120]})
        if not re.match(r"^\s*(case\b|\d+\s*->)", mln):  # leave switch/when labels alone
            for m in CONST_RE.finditer(mln):
                val = int(m.group(1).replace("_", ""))
                pos = starts[idx] + m.start()
                mutants.append({"line": nr, "op": "%d -> %d" % (val, val + 1), "start": pos,
                                "end": starts[idx] + m.end(), "rep": str(val + 1) + m.group(2),
                                "code": ln.strip()[:120]})
        if CALL_STMT_RE.match(mln) and not KEYWORD_START.match(mln) and "=" not in mln.split("(")[0]:
            mutants.append({"line": nr, "op": "remove call", "start": starts[idx], "end": starts[idx] + len(ln),
                            "rep": (";" if lang == "java" else ""), "code": ln.strip()[:120], "whole_line": True})
    return mutants


def pick(mutants, max_n):
    """Spread picks across lines and operator kinds: first one per line, then fill."""
    by_line = {}
    for m in mutants:
        by_line.setdefault(m["line"], []).append(m)
    chosen, rnd = [], 0
    while len(chosen) < max_n:
        added = False
        for line in sorted(by_line):
            lst = by_line[line]
            if rnd < len(lst):
                chosen.append(lst[rnd])
                added = True
                if len(chosen) >= max_n:
                    break
        if not added:
            break
        rnd += 1
    return sorted(chosen, key=lambda m: (m["line"], m["start"]))


def apply(text, m):
    if m.get("whole_line"):
        indent = re.match(r"\s*", text[m["start"]:m["end"]]).group(0)
        return text[:m["start"]] + indent + m["rep"] + text[m["end"]:]
    return text[:m["start"]] + m["rep"] + text[m["end"]:]


def state_paths(root):
    d = os.path.join(root, ".testgen")
    os.makedirs(os.path.join(d, "backup"), exist_ok=True)
    return d, os.path.join(d, "mutation.lock")


def restore(root, quiet=False):
    d, lock = state_paths(root)
    if not os.path.exists(lock):
        if not quiet:
            print("Nothing to restore.")
        return 0
    with open(lock, encoding="utf-8") as fh:
        info = json.load(fh)
    shutil.copyfile(info["backup"], info["file"])
    os.remove(lock)
    if not quiet:
        print("Restored %s" % info["file"])
    return 0


def covered_lines(root, path):
    """Lines of this source file that some test executes (from JaCoCo), or None if unknown."""
    data = cov.load_all(root)
    base = os.path.basename(path)
    lines = None
    for fq, e in data.items():
        if e.get("source") == base and e.get("lines"):
            pkg = e.get("package_dir", "")
            if pkg and pkg.replace("/", os.sep) not in path:
                continue
            lines = lines or set()
            lines.update(n for n, (mi, ci, mb, cb) in e["lines"].items() if ci > 0)
    return lines


def run_tests(root, module, tests, offline, timeout):
    cmd = [sys.executable, os.path.join(HERE, "run_tests.py"), root, "--module", module, "--timeout", str(timeout)]
    for t in tests:
        cmd += ["--tests", t]
    if offline:
        cmd.append("--offline")
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 60)
        return p.returncode, p.stdout, time.time() - t0
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT", time.time() - t0


def parse_range(s):
    if not s:
        return None
    out = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part.strip():
            out.add(int(part))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "list", "restore"])
    ap.add_argument("project")
    ap.add_argument("--file")
    ap.add_argument("--module", default=":app")
    ap.add_argument("--tests", action="append", default=[])
    ap.add_argument("--max", type=int, default=12)
    ap.add_argument("--lines", help="only mutate these lines, e.g. 20-80,95")
    ap.add_argument("--all-lines", action="store_true", help="also mutate lines no test executes")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--min-score", type=float, default=0.0)
    args = ap.parse_args()
    root = os.path.abspath(args.project)

    if args.command == "restore":
        return restore(root)
    if not args.file:
        print("--file is required")
        return 3
    path = args.file if os.path.isabs(args.file) else os.path.join(root, args.file)
    if not os.path.exists(path):
        print("No such file: %s" % path)
        return 3
    _, lock = state_paths(root)
    if os.path.exists(lock):
        print("A previous mutation run did not finish. Run `mutate.py restore %s` first." % root)
        return 3
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    lang = "java" if path.endswith(".java") else "kotlin"

    allowed = parse_range(args.lines)
    note = "all lines" if args.all_lines else ("lines %s" % args.lines if args.lines else "")
    if allowed is None and not args.all_lines:
        allowed = covered_lines(root, path)
        note = "covered lines only" if allowed is not None else "all lines (no coverage data)"
    mutants = generate(original, lang, allowed)
    chosen = pick(mutants, args.max)

    if args.command == "list":
        print("%d possible mutants (%s); would try %d:" % (len(mutants), note or "selected lines", len(chosen)))
        for m in chosen:
            print("  line %-4d %-14s %s" % (m["line"], m["op"], m["code"]))
        return 0
    if not args.tests:
        print("--tests is required for run")
        return 3
    if not chosen:
        print("No mutants to try (%s). Nothing executes this file yet, or it has no logic." % (note or "selected lines"))
        return 0

    print("Baseline: running %s on unmodified code..." % ", ".join(args.tests))
    code, out, secs = run_tests(root, args.module, args.tests, args.offline, args.timeout)
    if code != 0:
        print("Tests do not pass on the original code; fix them first.\n" + out[-2000:])
        return 3
    print("Baseline passed in %.0fs. Trying %d of %d mutants (%s). Each mutant recompiles, so expect a few "
          "times the baseline per mutant; a real estimate follows the first one."
          % (secs, len(chosen), len(mutants), note or "selected lines"))

    backup = os.path.join(root, ".testgen", "backup", os.path.basename(path) + ".orig")
    shutil.copyfile(path, backup)
    with open(lock, "w", encoding="utf-8") as fh:
        json.dump({"file": path, "backup": backup, "started": time.time()}, fh)

    def on_signal(signum, frame):
        restore(root, quiet=True)
        print("\nInterrupted; original file restored.")
        sys.exit(130)
    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_signal)

    results = []
    try:
        for i, m in enumerate(chosen, 1):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(apply(original, m))
            code, out, secs = run_tests(root, args.module, args.tests, args.offline, args.timeout)
            status = {0: "SURVIVED", 1: "KILLED", 2: "INVALID", 124: "TIMEOUT"}.get(code, "INVALID")
            m2 = {k: v for k, v in m.items() if k not in ("start", "end", "whole_line")}
            m2["status"] = status
            if status == "KILLED":
                fails = re.findall(r"^FAIL (\S+)", out, re.M)
                m2["killed_by"] = fails[:3]
            results.append(m2)
            print("[%d/%d] %-8s line %-4d %-14s %s (%.0fs)" % (i, len(chosen), status, m["line"], m["op"],
                                                              m["code"][:70], secs))
            if i == 1 and len(chosen) > 1:
                print("        (about %.1f min for the remaining %d mutants)" % (secs * (len(chosen) - 1) / 60,
                                                                           len(chosen) - 1))
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
    finally:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)
        if os.path.exists(lock):
            os.remove(lock)

    killed = sum(1 for r in results if r["status"] in ("KILLED", "TIMEOUT"))
    survived = [r for r in results if r["status"] == "SURVIVED"]
    valid = killed + len(survived)
    score = (100.0 * killed / valid) if valid else 100.0
    name = os.path.splitext(os.path.basename(path))[0]
    with open(os.path.join(root, ".testgen", "mutation-%s.json" % name), "w", encoding="utf-8") as fh:
        json.dump({"file": path, "tests": args.tests, "score": round(score, 1), "killed": killed,
                   "survived": len(survived), "invalid": len(results) - valid, "mutants": results}, fh, indent=2)
    print("\nMutation score: %.0f%% (%d killed, %d survived, %d invalid)" % (score, killed, len(survived),
                                                                            len(results) - valid))
    if survived:
        print("Survivors - each one is a behavior no test checks:")
        for r in survived:
            print("  line %-4d %-14s %s" % (r["line"], r["op"], r["code"]))
    return 0 if score >= args.min_score else 1


if __name__ == "__main__":
    sys.exit(main())
