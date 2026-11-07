#!/usr/bin/env python3
"""
run_tests.py - run specific tests through Gradle and print a short, LLM-friendly result.

Usage:
  python run_tests.py <project_root> --module :app --tests "com.example.FooViewModelTest"
        [--tests "com.example.BarTest"] [--variant debug] [--repeat 3] [--coverage]
        [--task testDebugUnitTest] [--module-dir app]

What it does:
  * picks the right task (testDebugUnitTest for Android modules, test for plain JVM modules)
  * forces the tests to actually re-run (clean<Task>) so cached results never hide a failure
  * on compile errors: prints only the compiler error lines (Kotlin "e:" / javac "error:")
  * on test failures: prints each failing test with its message and the project stack frames only
  * --repeat N: runs N times and reports flaky tests (pass sometimes, fail sometimes)
  * --coverage: also builds the JaCoCo report afterwards (create<Variant>UnitTestCoverageReport
    or jacocoTestReport / koverXmlReport, whichever exists)

Exit code: 0 all passed and stable, 1 test failures/flaky, 2 compile/build error, 3 setup problem.
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def gradlew(root):
    name = "gradlew.bat" if os.name == "nt" else "gradlew"
    p = os.path.join(root, name)
    if not os.path.exists(p):
        return None
    if os.name != "nt" and not os.access(p, os.X_OK):
        try:
            os.chmod(p, 0o755)
        except OSError:
            return ["sh", p]
    return [p]


def module_dir(root, module, override=None):
    if override:
        return os.path.join(root, override)
    rel = module.strip(":").replace(":", os.sep)
    return os.path.join(root, rel) if rel else root


def is_android(mdir):
    for f in ("build.gradle.kts", "build.gradle"):
        p = os.path.join(mdir, f)
        if os.path.exists(p):
            t = open(p, encoding="utf-8", errors="ignore").read()
            return "com.android" in t or re.search(r"\bandroid\s*[{(]", t) is not None
    return True


def supports_rerun(root):
    try:
        with open(os.path.join(root, "gradle", "wrapper", "gradle-wrapper.properties"), encoding="utf-8") as fh:
            m = re.search(r"gradle-(\d+)\.(\d+)", fh.read())
        return bool(m) and (int(m.group(1)), int(m.group(2))) >= (7, 6)
    except OSError:
        return False


def cap(s):
    return s[:1].upper() + s[1:]


def pick_task(mdir, variant, task):
    if task:
        return task
    return "test%sUnitTest" % cap(variant) if is_android(mdir) else "test"


def run_gradle(cmd, root, timeout):
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=timeout)
        out = p.stdout + "\n" + p.stderr
        return p.returncode, out, time.time() - t0
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        return 124, out + "\nTIMEOUT after %ss" % timeout, time.time() - t0


COMPILE_PATTERNS = [
    re.compile(r"^e: .*"),                       # kotlinc
    re.compile(r"^.*\.java:\d+: error: .*"),     # javac
    re.compile(r"^.*error: .*"),
    re.compile(r"^> Task .* FAILED"),
    re.compile(r"^\s*(Unresolved reference|Type mismatch|None of the following|Cannot access).*"),
]


def compile_errors(output, limit=40):
    lines = []
    for ln in output.splitlines():
        s = ln.rstrip()
        if any(p.match(s) for p in COMPILE_PATTERNS):
            if s not in lines:
                lines.append(s)
    # javac prints the offending source line + caret after the error; good enough without it
    return lines[:limit]


def build_failure_reason(output):
    m = re.search(r"\* What went wrong:\n(.*?)(?:\n\* Try:|\Z)", output, re.S)
    return m.group(1).strip()[:2000] if m else None


def parse_results(mdir, task, since):
    """Read JUnit XML written by this run (mtime >= since)."""
    res = {}
    pattern = os.path.join(mdir, "build", "test-results", task, "*.xml")
    for f in glob.glob(pattern):
        if os.path.getmtime(f) + 1 < since:
            continue
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for tc in root.iter("testcase"):
            name = "%s.%s" % (tc.get("classname"), tc.get("name"))
            fail = tc.find("failure")
            if fail is None:
                fail = tc.find("error")
            if tc.find("skipped") is not None:
                res[name] = ("skipped", None)
            elif fail is not None:
                msg = (fail.get("message") or "")
                body = fail.text or ""
                res[name] = ("failed", (msg, body))
            else:
                res[name] = ("passed", None)
    return res


def clear_results(mdir, task):
    """Remove old JUnit XML so a failed compile can never be read as a fresh result."""
    for f in glob.glob(os.path.join(mdir, "build", "test-results", task, "*.xml")):
        try:
            os.remove(f)
        except OSError:
            pass


def short_stack(body, project_pkgs, max_frames=8):
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("at "):
            if any(("at " + p) in s for p in project_pkgs) or not project_pkgs:
                out.append("    " + s)
        elif s.startswith("Caused by") or not out:
            out.append("  " + s[:400])
        if len(out) >= max_frames:
            break
    return "\n".join(out)


def coverage_task(root, gw, module, variant, mdir):
    candidates = ["create%sUnitTestCoverageReport" % cap(variant), "jacocoTestReport", "koverXmlReport%s" % cap(variant),
                  "koverXmlReport"]
    prefix = "" if module == ":" else module
    for t in candidates:
        code, out, _ = run_gradle(gw + ["-q", "help", "--task", "%s:%s" % (prefix, t)], root, 300)
        if code == 0:
            return t
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project")
    ap.add_argument("--module", default=":app")
    ap.add_argument("--module-dir", help="folder of the module if it doesn't match the gradle path")
    ap.add_argument("--tests", action="append", default=[], help="test filter, repeatable (class or class.method)")
    ap.add_argument("--variant", default="debug")
    ap.add_argument("--task", help="override the test task name")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.project)
    gw = gradlew(root)
    if not gw:
        print("SETUP: no gradlew found in %s" % root)
        return 3
    mdir = module_dir(root, args.module, args.module_dir)
    if not os.path.isdir(mdir):
        print("SETUP: module folder %s not found (use --module-dir)" % mdir)
        return 3
    task = pick_task(mdir, args.variant, args.task)
    mod = args.module if args.module.startswith(":") else ":" + args.module
    prefix = "" if mod == ":" else mod
    pkgs = sorted({".".join(t.split(".")[:2]) for t in args.tests if t.count(".") >= 2})

    base = gw + ["--console=plain", "--build-cache"]
    if args.offline:
        base.append("--offline")
    filters = []
    for t in args.tests:
        filters += ["--tests", t]

    history = {}
    for i in range(args.repeat):
        if supports_rerun(root):
            cmd = base + ["%s:%s" % (prefix, task), "--rerun"] + filters  # Gradle 7.6+: re-run only this task
        else:
            cmd = base + ["%s:clean%s" % (prefix, cap(task)), "%s:%s" % (prefix, task)] + filters
        clear_results(mdir, task)
        started = time.time()
        code, out, secs = run_gradle(cmd, root, args.timeout)
        results = parse_results(mdir, task, started)
        if code != 0 and not results:
            errs = compile_errors(out)
            print("BUILD FAILED (run %d, %.0fs)" % (i + 1, secs))
            if errs:
                print("Compiler errors:")
                print("\n".join(errs))
            else:
                reason = build_failure_reason(out)
                print(reason or "\n".join(out.splitlines()[-40:]))
            if "No tests found for given includes" in out:
                print("\nHint: the --tests filter matched nothing. Check package/class name and source set.")
            return 2
        for name, (status, info) in results.items():
            history.setdefault(name, []).append((status, info))
        failed = [n for n, (s, _) in results.items() if s == "failed"]
        print("run %d/%d: %d tests, %d failed (%.0fs)" % (i + 1, args.repeat, len(results), len(failed), secs))

    always_fail, flaky, passed = [], [], []
    for name, runs in sorted(history.items()):
        st = {s for s, _ in runs if s != "skipped"}
        if st == {"failed"}:
            always_fail.append(name)
        elif st == {"failed", "passed"}:
            flaky.append(name)
        elif st == {"passed"}:
            passed.append(name)

    print("\nPASSED: %d | FAILED: %d | FLAKY: %d" % (len(passed), len(always_fail), len(flaky)))
    for name in always_fail:
        msg, body = next(info for s, info in history[name] if s == "failed")
        print("\nFAIL %s\n  %s" % (name, (msg or "").strip()[:600]))
        st = short_stack(body or "", pkgs)
        if st:
            print(st)
    for name in flaky:
        n_fail = sum(1 for s, _ in history[name] if s == "failed")
        print("\nFLAKY %s (failed %d of %d runs) - fix or delete; do not keep" % (name, n_fail, len(history[name])))

    if args.coverage and not always_fail:
        t = coverage_task(root, gw, mod, args.variant, mdir)
        if not t:
            print("\nCOVERAGE: no coverage task found. Enable it (references/gradle-setup.md).")
        else:
            code, out, secs = run_gradle(base + ["%s:%s" % (prefix, t)], root, args.timeout)
            print("\nCOVERAGE: %s %s (%.0fs)" % (t, "ok" if code == 0 else "FAILED", secs))
            if code != 0:
                print(build_failure_reason(out) or "\n".join(out.splitlines()[-25:]))

    if always_fail or flaky:
        return 1
    if not history:
        print("No test results were written. Is the filter right, and is the test under src/test?")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
