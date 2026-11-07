#!/usr/bin/env python3
"""
scan.py - find and rank the classes in an Android project that are worth testing.

Works on mixed Kotlin/Java projects with plain heuristics (no compiler needed),
so it runs in seconds on large repos.

Usage:
  python scan.py <project_root> [--module app] [--top 20] [--changed-since main]
                 [--include-tested] [--json out.json]

For each production class it reports:
  kind         ViewModel / UseCase / Repository / Dao / Util / UI ...
  test_type    which kind of test fits (jvm-unit, robolectric, room, compose-ui, espresso, skip)
  complexity   rough count of decision points
  churn        number of commits touching the file (last 6 months, needs git)
  coverage     line coverage % from the latest JaCoCo XML report, if one exists
  has_test     whether a <Name>Test file already exists
  smells       things that make the class hard to test (hard-coded dispatchers, clocks, ...)
  score        priority = complexity * (1 - coverage) * churn factor * kind weight
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coverage as cov  # noqa: E402

SKIP_DIRS = {"build", ".gradle", ".git", ".idea", "node_modules", ".testgen", "generated", ".cxx"}
SRC_EXT = (".kt", ".java")

DECISION_RE = re.compile(r"\b(if|when|for|while|catch|case)\b|&&|\|\||\?:|\?\.let")
PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
CLASS_RE = re.compile(
    r"^[ \t]*((?:public|private|internal|protected|abstract|open|final|sealed|data|enum|"
    r"inner|value|annotation|static|@\w+(?:\([^)]*\))?)\s+)*"
    r"(class|object|interface)\s+(\w+)",
    re.M,
)
FUN_RE = re.compile(r"\bfun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?(\w+)\s*\(|"
                    r"(?:public|protected|private|static|\s)+[\w<>\[\],\s]+\s+(\w+)\s*\([^;]*\)\s*(?:throws[^{]*)?\{")

SMELLS = [
    (re.compile(r"Dispatchers\.(IO|Default|Main)\b"), "hard-coded dispatcher (inject a CoroutineDispatcher)"),
    (re.compile(r"System\.currentTimeMillis|LocalDateTime\.now|Instant\.now|new Date\(\)|Calendar\.getInstance"),
     "reads the real clock (inject a Clock / time provider)"),
    (re.compile(r"GlobalScope\."), "uses GlobalScope (pass a CoroutineScope in)"),
    (re.compile(r"\bRandom\(|Math\.random|UUID\.randomUUID"), "uses randomness directly (inject a generator)"),
    (re.compile(r"\.getInstance\(\)"), "pulls a singleton via getInstance() (inject it instead)"),
    (re.compile(r"Log\.[dewiv]\("), None),  # android.util.Log - fine with isReturnDefaultValues, not a smell
]

ANDROID_UI_SUPERS = ("Activity", "Fragment", "AppCompatActivity", "ComponentActivity", "DialogFragment",
                     "BottomSheetDialogFragment", "View", "RecyclerView.Adapter", "ListAdapter", "Service",
                     "BroadcastReceiver", "ContentProvider", "Application")

KIND_WEIGHT = {
    "ViewModel": 1.6, "UseCase": 1.6, "Repository": 1.4, "Mapper": 1.2, "Validator": 1.4,
    "Util": 1.1, "Presenter": 1.5, "Reducer": 1.6, "DataSource": 1.0, "Dao": 0.9, "Worker": 0.8,
    "Class": 1.0, "Composable": 0.5, "AndroidUI": 0.3, "Adapter": 0.5, "Model": 0.2,
}

TEST_TYPE = {
    "ViewModel": "jvm-unit", "UseCase": "jvm-unit", "Repository": "jvm-unit", "Mapper": "jvm-unit",
    "Validator": "jvm-unit", "Util": "jvm-unit", "Presenter": "jvm-unit", "Reducer": "jvm-unit",
    "DataSource": "jvm-unit", "Class": "jvm-unit", "Dao": "room", "Worker": "robolectric",
    "Composable": "compose-ui", "AndroidUI": "espresso-or-robolectric", "Adapter": "robolectric",
    "Model": "skip",
}


def find_modules(root, only=None):
    mods = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if ("build.gradle" in filenames or "build.gradle.kts" in filenames) and os.path.isdir(
                os.path.join(dirpath, "src")):
            rel = os.path.relpath(dirpath, root)
            gradle_path = ":" + rel.replace(os.sep, ":") if rel != "." else ":"
            if only and only.strip(":") not in (rel, gradle_path.strip(":"), os.path.basename(dirpath)):
                continue
            build_file = os.path.join(dirpath, "build.gradle.kts" if "build.gradle.kts" in filenames else "build.gradle")
            try:
                build_txt = open(build_file, encoding="utf-8", errors="ignore").read()
            except OSError:
                build_txt = ""
            mods.append({
                "dir": dirpath, "rel": rel, "gradle_path": gradle_path,
                "android": "com.android" in build_txt or "android {" in build_txt or "android(" in build_txt,
                "kotlin": "kotlin" in build_txt.lower(),
                "build_file": build_file,
            })
    return mods


def walk_sources(base):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith(SRC_EXT):
                yield os.path.join(dirpath, f)


def source_sets(mod_dir, kind):
    """kind: 'main' | 'test' | 'androidTest'"""
    src = os.path.join(mod_dir, "src")
    if not os.path.isdir(src):
        return []
    out = []
    for name in os.listdir(src):
        is_test = name.lower().startswith("test") or name.endswith("Test") and not name.startswith("android")
        is_android_test = name.startswith("androidTest")
        if kind == "main" and not is_test and not is_android_test and name not in ("sharedTest", "testFixtures"):
            out.append(os.path.join(src, name))
        elif kind == "test" and (is_test or name in ("sharedTest", "testFixtures")):
            out.append(os.path.join(src, name))
        elif kind == "androidTest" and is_android_test:
            out.append(os.path.join(src, name))
    return out


_TOKEN_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*[\s\S]*?\*/|//[^\n]*')


def strip_comments_and_strings(text):
    """Blank out comments and string contents in one pass (so "http://" in a string is not a comment)."""
    def repl(m):
        t = m.group(0)
        if t.startswith(("/*", "//")):
            return "\n" * t.count("\n")
        return '""'
    return _TOKEN_RE.sub(repl, text)


def classify(name, text, decl_kind, modifiers):
    n = name
    if decl_kind == "interface":
        return None
    if "@Module" in text and ("@InstallIn" in text or "@Provides" in text or "@Binds" in text):
        return None  # DI wiring - nothing to test
    if "@Dao" in text:
        return "Dao"
    if "@Entity" in text or ("data" in modifiers and not re.search(r"\bfun\s+\w+", text)):
        return "Model"
    if "enum" in modifiers:
        return "Model" if not re.search(r"\bfun\s+\w+", text) else "Util"
    if n.endswith("ViewModel") or re.search(r":\s*(Android)?ViewModel\(", text) or "extends ViewModel" in text \
            or "extends AndroidViewModel" in text:
        return "ViewModel"
    if n.endswith(("UseCase", "Interactor")):
        return "UseCase"
    if n.endswith(("Repository", "RepositoryImpl", "Repo", "RepoImpl")):
        return "Repository"
    if n.endswith(("DataSource", "DataSourceImpl", "Api", "Service")) and not re.search(r":\s*Service\(\)|extends Service", text):
        return "DataSource"
    if n.endswith(("Mapper", "Converter", "Transformer", "Formatter", "Parser")):
        return "Mapper"
    if n.endswith(("Validator", "Checker", "Policy", "Rules", "Calculator")):
        return "Validator"
    if n.endswith(("Presenter",)):
        return "Presenter"
    if n.endswith(("Reducer", "StateMachine")):
        return "Reducer"
    if n.endswith("Worker") or "CoroutineWorker" in text or "extends Worker" in text:
        return "Worker"
    if n.endswith("Adapter"):
        return "Adapter"
    for sup in ANDROID_UI_SUPERS:
        if re.search(r"(:|extends)\s*" + re.escape(sup) + r"\b", text):
            return "AndroidUI"
    if n.endswith(("Activity", "Fragment", "Screen", "View", "Dialog")) and "@Composable" not in text:
        return "AndroidUI"
    if "@Composable" in text:
        return "Composable"
    if n.endswith(("Utils", "Util", "Helper", "Extensions", "Ext", "Kt")):
        return "Util"
    return "Class"


def git_churn(root, since="6.months"):
    try:
        out = subprocess.run(["git", "-C", root, "log", "--since=" + since, "--name-only", "--format="],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None
    counts = {}
    top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    git_root = top.stdout.strip() or root
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.endswith(SRC_EXT):
            p = os.path.normpath(os.path.join(git_root, line))
            counts[p] = counts.get(p, 0) + 1
    return counts


def changed_files(root, ref):
    files = set()
    cmds = [["git", "-C", root, "diff", "--name-only", ref + "...HEAD"],
            ["git", "-C", root, "diff", "--name-only"],
            ["git", "-C", root, "diff", "--name-only", "--cached"],
            ["git", "-C", root, "ls-files", "--others", "--exclude-standard"]]
    top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    git_root = top.stdout.strip() or root
    for c in cmds:
        r = subprocess.run(c, capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.strip().endswith(SRC_EXT):
                    files.add(os.path.normpath(os.path.join(git_root, line.strip())))
    return files


def existing_test_names(mod):
    names = {}
    for kind in ("test", "androidTest"):
        for base in source_sets(mod["dir"], kind):
            for f in walk_sources(base):
                stem = os.path.splitext(os.path.basename(f))[0]
                names.setdefault(stem, []).append(os.path.relpath(f, mod["dir"]))
    return names


def _balanced(text, i):
    """text[i] == '(' -> index of the matching ')'."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return -1


def _split_params(s):
    out, depth, cur = [], 0, ""
    for c in s:
        if c in "(<[{":
            depth += 1
        elif c in ")>]}":
            depth -= 1
        if c == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += c
    if cur.strip():
        out.append(cur)
    return out


def _base_type(t):
    t = re.sub(r"<.*", "", t.strip()).rstrip("?").strip()
    t = t.split(".")[-1]
    return t if re.match(r"^[A-Z]\w*$", t) else None


PRIMITIVES = {"String", "Int", "Long", "Boolean", "Double", "Float", "Short", "Byte", "Char", "Unit", "Any",
              "Integer", "Object", "List", "Map", "Set", "MutableList", "MutableMap", "Array", "SavedStateHandle"}


def constructor_deps(text, name, lang):
    """Types injected through the constructor (primary/secondary in Kotlin, constructors in Java)."""
    deps = []
    if lang == "kotlin":
        m = re.search(r"\bclass\s+" + re.escape(name) + r"\b[^({\n]*?(?:constructor\s*)?\(", text)
        if m:
            end = _balanced(text, m.end() - 1)
            for prm in _split_params(text[m.end():end]):
                prm = prm.split("=")[0]
                if ":" in prm:
                    deps.append(_base_type(prm.split(":", 1)[1]))
    else:
        for m in re.finditer(r"(?:public|protected|private|\s)\s*" + re.escape(name) + r"\s*\(", text):
            end = _balanced(text, m.end() - 1)
            for prm in _split_params(text[m.end():end]):
                parts = re.sub(r"@\w+(\([^)]*\))?|\bfinal\b", "", prm).split()
                if len(parts) >= 2:
                    deps.append(_base_type(" ".join(parts[:-1])))
    seen = []
    for d in deps:
        if d and d not in PRIMITIVES and d not in seen:
            seen.append(d)
    return seen


def analyze_file(path, mod, tests, churn, coverage_map):
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    if "@Generated" in raw[:2000] or "/build/generated/" in path.replace(os.sep, "/"):
        return []
    text = strip_comments_and_strings(raw)
    pkg_m = PACKAGE_RE.search(raw)
    pkg = pkg_m.group(1) if pkg_m else ""
    stem = os.path.splitext(os.path.basename(path))[0]
    decls = [(m.group(3), m.group(2), m.group(1) or "") for m in CLASS_RE.finditer(text)]
    # pick the primary declaration: same name as file, else first non-interface, else file-level functions
    primary = None
    for name, kind, mods in decls:
        if name == stem:
            primary = (name, kind, mods)
            break
    if primary is None:
        non_iface = [d for d in decls if d[1] != "interface"]
        if non_iface:
            primary = non_iface[0]
        elif path.endswith(".kt") and re.search(r"^fun\s", text, re.M):
            primary = (stem + "Kt", "class", "")  # top-level functions compile to FileNameKt
        else:
            return []
    name, decl_kind, modifiers = primary
    kind = classify(name, text, decl_kind, modifiers)
    if kind is None:
        return []
    complexity = len(DECISION_RE.findall(text))
    functions = len(re.findall(r"\bfun\s", text)) if path.endswith(".kt") else len(
        re.findall(r"(?:public|protected|private|static)[^;{=]*\([^;{]*\)\s*(?:throws[^{]*)?\{", text))
    smells = []
    for rx, msg in SMELLS:
        if msg and rx.search(text):
            smells.append(msg)
    if kind in ("ViewModel", "UseCase", "Repository", "Mapper", "Validator", "Util", "Class", "Presenter",
                "Reducer") and re.search(r"\bContext\b", text) and "ApplicationProvider" not in text:
        smells.append("depends on android Context in non-UI code (needs Robolectric or an abstraction)")
    if re.search(r"=\s*\w+(Impl|Repository|Api|Service|Client)\(", text) and kind != "Model":
        smells.append("creates its own dependencies (constructor-inject them)")
    fq = (pkg + "." if pkg else "") + name
    test_files = tests.get(name + "Test", []) + tests.get(name + "Tests", []) + tests.get(name + "Spec", [])
    c = coverage_map.get(fq) if coverage_map else None
    line_cov = c["line_pct"] if c else None
    churn_n = churn.get(os.path.normpath(path), 0) if churn else 0
    cov_factor = 1.0 - (line_cov / 100.0 if line_cov is not None else (0.6 if test_files else 0.0))
    score = (1 + complexity + 0.5 * functions) * max(cov_factor, 0.05) * (1 + math.log1p(churn_n)) \
        * KIND_WEIGHT.get(kind, 1.0)
    if len(smells) >= 3:
        score *= 0.6
    return [{
        "class": fq, "name": name, "file": os.path.relpath(path, os.path.dirname(mod["dir"])) if mod["rel"] == "."
        else os.path.relpath(path, mod["dir"]), "abs_path": path, "module": mod["gradle_path"],
        "lang": "kotlin" if path.endswith(".kt") else "java", "kind": kind,
        "test_type": TEST_TYPE.get(kind, "jvm-unit"), "complexity": complexity, "functions": functions,
        "churn": churn_n, "coverage": line_cov, "has_test": bool(test_files), "existing_tests": test_files,
        "smells": smells, "testability": "needs-refactor" if len(smells) >= 3 else (
            "testable-with-fakes" if smells else "testable"),
        "deps": constructor_deps(text, name, "kotlin" if path.endswith(".kt") else "java"),
        "score": round(score, 1),
    }]


def project_index(mods):
    """Interfaces declared in production code, and test doubles that already exist in test code."""
    interfaces, fakes = set(), {}
    iface_re = re.compile(r"(?<!fun )\binterface\s+([A-Z]\w*)")  # fun interfaces are stubbed with a lambda
    impl_re = re.compile(r"\bclass\s+((?:Fake|Stub|InMemory|Test)\w*)\b[^{\n]*?(?::|implements|extends)\s*([A-Z]\w*)")
    for mod in mods:
        for base in source_sets(mod["dir"], "main"):
            for f in walk_sources(base):
                try:
                    t = open(f, encoding="utf-8", errors="ignore").read()
                except OSError:
                    continue
                interfaces.update(iface_re.findall(t))
        for kind in ("test", "androidTest"):
            for base in source_sets(mod["dir"], kind):
                for f in walk_sources(base):
                    try:
                        t = open(f, encoding="utf-8", errors="ignore").read()
                    except OSError:
                        continue
                    for fake, iface in impl_re.findall(t):
                        fakes.setdefault(iface, []).append(fake)
    return interfaces, fakes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--module", help="only this module (e.g. app or :feature:login)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--changed-since", help="only files changed vs this git ref (plus uncommitted work)")
    ap.add_argument("--include-tested", action="store_true", help="keep classes that already have a test file")
    ap.add_argument("--include-ui", action="store_true", help="keep Activities/Fragments/Composables in the list")
    ap.add_argument("--json", help="write full results to this file")
    ap.add_argument("--fail-untested", action="store_true",
                    help="exit 1 if any listed target has no test file (for git hooks / CI)")
    args = ap.parse_args()

    root = os.path.abspath(args.project)
    mods = find_modules(root, args.module)
    if not mods:
        print("No Gradle modules with a src/ folder found under " + root)
        return 2
    churn = git_churn(root)
    coverage_map = cov.load_class_coverage(root)
    only = changed_files(root, args.changed_since) if args.changed_since else None

    results = []
    for mod in mods:
        tests = existing_test_names(mod)
        for base in source_sets(mod["dir"], "main"):
            for f in walk_sources(base):
                if only is not None and os.path.normpath(f) not in only:
                    continue
                results.extend(analyze_file(f, mod, tests, churn, coverage_map))

    interfaces, fakes = project_index(mods)
    for r in results:
        r["fakes_needed"] = [d for d in r["deps"] if d in interfaces and d not in fakes]
        r["fakes_existing"] = {d: fakes[d] for d in r["deps"] if d in fakes}
    all_count = len(results)
    shown = [r for r in results if r["kind"] != "Model"]
    if not args.include_ui:
        shown = [r for r in shown if r["kind"] not in ("AndroidUI", "Composable", "Adapter")]
    if not args.include_tested and not args.changed_since:
        shown = [r for r in shown if not r["has_test"] or (r["coverage"] is not None and r["coverage"] < 60)]
    shown.sort(key=lambda r: r["score"], reverse=True)

    summary = {
        "project": root,
        "modules": [{"path": m["gradle_path"], "android": m["android"], "kotlin": m["kotlin"]} for m in mods],
        "classes_found": all_count,
        "coverage_report_found": bool(coverage_map),
        "git_history": churn is not None,
        "targets": shown,
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)

    print("Project: %s" % root)
    print("Modules: %s" % ", ".join("%s%s" % (m["gradle_path"], "" if m["android"] else " (jvm)") for m in mods))
    print("Production classes scanned: %d | coverage report: %s | git history: %s"
          % (all_count, "yes" if coverage_map else "no (run coverage first for better ranking)",
             "yes" if churn is not None else "no"))
    if args.changed_since:
        print("Limited to files changed since %s" % args.changed_since)
    print()
    print("%-4s %-6s %-44s %-11s %-13s %5s %5s %6s  %s" % ("#", "score", "class", "kind", "test type", "cx",
                                                           "churn", "cov%", "testability"))
    for i, r in enumerate(shown[: args.top], 1):
        cv = "-" if r["coverage"] is None else "%.0f" % r["coverage"]
        nm = r["class"] if len(r["class"]) <= 44 else "..." + r["class"][-41:]
        print("%-4d %-6s %-44s %-11s %-13s %5d %5d %6s  %s%s" % (
            i, r["score"], nm, r["kind"], r["test_type"], r["complexity"], r["churn"], cv, r["testability"],
            " (has test)" if r["has_test"] else ""))
    refactor = [r for r in shown if r["smells"]][:10]
    if refactor:
        print("\nTestability notes:")
        for r in refactor:
            print("  %s: %s" % (r["name"], "; ".join(r["smells"])))
    # fakes plan: interfaces the top targets depend on, shared fakes first
    need = {}
    for r in shown[: args.top]:
        for d in r["fakes_needed"]:
            need.setdefault(d, []).append(r["name"])
    have = {}
    for r in shown[: args.top]:
        for d, fs in r["fakes_existing"].items():
            have[d] = fs
    if need or have:
        print("\nFakes plan (write shared fakes before tests; most-used first):")
        for d, users in sorted(need.items(), key=lambda kv: -len(kv[1])):
            print("  NEED  Fake%s  <- %s" % (d.replace("Impl", ""), ", ".join(users[:6])))
        for d, fs in sorted(have.items()):
            print("  have  %s for %s" % (", ".join(sorted(set(fs))), d))
    summary["fakes_needed"] = need
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
    if len(shown) > args.top:
        print("\n(%d more targets; use --top or --json to see all)" % (len(shown) - args.top))
    if args.fail_untested:
        missing = [r for r in shown if not r["has_test"]]
        if missing:
            print("\nUNTESTED: %d changed class(es) have no tests: %s" % (
                len(missing), ", ".join(r["name"] for r in missing[:15])))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
