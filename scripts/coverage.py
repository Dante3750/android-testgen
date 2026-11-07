#!/usr/bin/env python3
"""
coverage.py - read JaCoCo XML reports (works for Kotlin and Java) and answer:
  which classes are poorly covered, which exact lines/branches are uncovered,
  and how coverage changed between two snapshots.

Usage:
  python coverage.py summary  <project_root> [--top 30]
  python coverage.py class    <project_root> com.example.FooViewModel
  python coverage.py snapshot <project_root> <name>          # saves .testgen/coverage-<name>.json
  python coverage.py diff     <project_root> <before> <after> # compares two snapshots
  python coverage.py reports  <project_root>                  # lists the XML reports found

Reports are found automatically under **/build/reports/** (AGP's
createDebugUnitTestCoverageReport, the jacoco plugin, or Kover's XML report).
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

SKIP_DIRS = {".git", ".gradle", "node_modules", ".idea", "intermediates", "tmp", "outputs"}


def find_reports(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        norm = dirpath.replace(os.sep, "/")
        if "/build/reports" not in norm and "/build/jacoco" not in norm and "/build/kover" not in norm:
            continue
        for f in filenames:
            if not f.endswith(".xml"):
                continue
            p = os.path.join(dirpath, f)
            try:
                with open(p, "rb") as fh:
                    head = fh.read(600)
            except OSError:
                continue
            if b"<report" in head and (b"JACOCO" in head.upper() or b"<sessioninfo" in head or b"<package" in head):
                found.append(p)
    # newest first
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def _counter(el, kind):
    for c in el.findall("counter"):
        if c.get("type") == kind:
            return int(c.get("missed", 0)), int(c.get("covered", 0))
    return 0, 0


def _outer(name):
    # com/x/Foo$Bar$1 -> com.x.Foo ; also strip Kotlin lambdas/companions
    return name.split("$")[0].replace("/", ".")


def parse_report(path):
    """Return {fq_class: {line_missed, line_covered, br_missed, br_covered, source, package_dir, lines:{nr:(mi,ci,mb,cb)}}}"""
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {}
    out = {}
    for pkg in tree.getroot().iter("package"):
        pdir = pkg.get("name", "")
        src_lines = {}
        for sf in pkg.findall("sourcefile"):
            lines = {}
            for ln in sf.findall("line"):
                lines[int(ln.get("nr"))] = (int(ln.get("mi", 0)), int(ln.get("ci", 0)),
                                            int(ln.get("mb", 0)), int(ln.get("cb", 0)))
            src_lines[sf.get("name")] = lines
        for cl in pkg.findall("class"):
            fq = _outer(cl.get("name", ""))
            lm, lc = _counter(cl, "LINE")
            bm, bc = _counter(cl, "BRANCH")
            e = out.setdefault(fq, {"line_missed": 0, "line_covered": 0, "br_missed": 0, "br_covered": 0,
                                    "source": cl.get("sourcefilename"), "package_dir": pdir})
            e["line_missed"] += lm
            e["line_covered"] += lc
            e["br_missed"] += bm
            e["br_covered"] += bc
        for fq, e in out.items():
            if e.get("package_dir") == pdir and "lines" not in e and e.get("source") in src_lines:
                e["lines"] = src_lines[e["source"]]
    return out


def _pct(m, c):
    return round(100.0 * c / (m + c), 1) if (m + c) else None


def load_all(root):
    """Merge every report found (newest wins per class)."""
    merged = {}
    for rep in reversed(find_reports(root)):
        merged.update(parse_report(rep))
    for e in merged.values():
        e["line_pct"] = _pct(e["line_missed"], e["line_covered"])
        e["branch_pct"] = _pct(e["br_missed"], e["br_covered"])
    return merged


def load_class_coverage(root):
    data = load_all(root)
    return {k: {"line_pct": v["line_pct"] if v["line_pct"] is not None else 0.0, "branch_pct": v["branch_pct"]}
            for k, v in data.items()}


def _ranges(nums):
    nums = sorted(nums)
    out, start, prev = [], None, None
    for n in nums:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append((start, prev))
            start = prev = n
    if start is not None:
        out.append((start, prev))
    return ", ".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in out)


def totals(data):
    lm = sum(v["line_missed"] for v in data.values())
    lc = sum(v["line_covered"] for v in data.values())
    bm = sum(v["br_missed"] for v in data.values())
    bc = sum(v["br_covered"] for v in data.values())
    return {"line_pct": _pct(lm, lc), "branch_pct": _pct(bm, bc), "lines": lm + lc, "classes": len(data)}


def cmd_summary(root, top=30):
    data = load_all(root)
    if not data:
        print("No JaCoCo/Kover XML report found under %s. Run the coverage task first "
              "(see references/gradle-setup.md)." % root)
        return 1
    t = totals(data)
    print("Overall: line %s%% | branch %s%% | %d classes, %d lines" % (t["line_pct"], t["branch_pct"],
                                                                     t["classes"], t["lines"]))
    rows = sorted(data.items(), key=lambda kv: kv[1]["line_missed"], reverse=True)
    print("\nMost uncovered lines:")
    print("%-60s %8s %8s %8s" % ("class", "line%", "branch%", "missed"))
    for fq, v in rows[:top]:
        if v["line_missed"] == 0:
            break
        print("%-60s %8s %8s %8d" % (fq[-60:], v["line_pct"], v["branch_pct"] if v["branch_pct"] is not None else "-",
                                     v["line_missed"]))
    return 0


def cmd_class(root, fq):
    data = load_all(root)
    e = data.get(fq)
    if e is None:
        cands = [k for k in data if k.endswith("." + fq) or k == fq or k.split(".")[-1] == fq]
        if len(cands) == 1:
            fq, e = cands[0], data[cands[0]]
        elif cands:
            print("Ambiguous, did you mean: " + ", ".join(cands))
            return 1
    if e is None:
        print("%s not in coverage report (never loaded by any test, or report is stale)." % fq)
        return 1
    print("%s  (%s)" % (fq, e.get("source")))
    print("line %s%% (%d/%d) | branch %s%% (%d/%d)" % (
        e["line_pct"], e["line_covered"], e["line_missed"] + e["line_covered"],
        e["branch_pct"] if e["branch_pct"] is not None else "-", e["br_covered"], e["br_missed"] + e["br_covered"]))
    lines = e.get("lines", {})
    uncovered = [n for n, (mi, ci, mb, cb) in lines.items() if mi > 0 and ci == 0]
    partial = [(n, mb, cb) for n, (mi, ci, mb, cb) in lines.items() if mb > 0 and ci > 0]
    if uncovered:
        print("uncovered lines: " + _ranges(uncovered))
    if partial:
        print("partially covered branches: " + ", ".join(
            "%d (%d of %d branches missed)" % (n, mb, mb + cb) for n, mb, cb in sorted(partial)))
    if not uncovered and not partial:
        print("fully covered")
    print("(line numbers refer to %s; note a source file can hold several classes)" % e.get("source"))
    return 0


def _state_dir(root):
    d = os.path.join(root, ".testgen")
    os.makedirs(d, exist_ok=True)
    return d


def cmd_snapshot(root, name):
    data = load_all(root)
    if not data:
        print("No coverage report found; nothing to snapshot.")
        return 1
    slim = {k: {kk: v[kk] for kk in ("line_missed", "line_covered", "br_missed", "br_covered", "line_pct",
                                     "branch_pct")} for k, v in data.items()}
    path = os.path.join(_state_dir(root), "coverage-%s.json" % name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"totals": totals(data), "classes": slim}, fh, indent=1)
    t = totals(data)
    print("Saved %s  (line %s%%, branch %s%%)" % (path, t["line_pct"], t["branch_pct"]))
    return 0


def cmd_diff(root, a, b):
    def load(n):
        p = os.path.join(root, ".testgen", "coverage-%s.json" % n)
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    A, B = load(a), load(b)
    ta, tb = A["totals"], B["totals"]
    print("Overall line   %s%% -> %s%%" % (ta["line_pct"], tb["line_pct"]))
    print("Overall branch %s%% -> %s%%" % (ta["branch_pct"], tb["branch_pct"]))
    changes = []
    for k, vb in B["classes"].items():
        va = A["classes"].get(k, {"line_pct": 0.0, "branch_pct": 0.0})
        d = (vb["line_pct"] or 0) - (va["line_pct"] or 0)
        if abs(d) >= 0.1:
            changes.append((d, k, va["line_pct"], vb["line_pct"], va.get("branch_pct"), vb.get("branch_pct")))
    changes.sort(reverse=True)
    if changes:
        print("\n%-60s %16s %16s" % ("class", "line%", "branch%"))
        for d, k, la, lb, ba, bb in changes:
            print("%-60s %7s -> %-6s %7s -> %-6s" % (k[-60:], la, lb, ba, bb))
    else:
        print("No class-level changes.")
    return 0


def main(argv):
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, root = argv[0], os.path.abspath(argv[1])
    if cmd == "summary":
        top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 30
        return cmd_summary(root, top)
    if cmd == "class" and len(argv) >= 3:
        return cmd_class(root, argv[2])
    if cmd == "snapshot" and len(argv) >= 3:
        return cmd_snapshot(root, argv[2])
    if cmd == "diff" and len(argv) >= 4:
        return cmd_diff(root, argv[2], argv[3])
    if cmd == "reports":
        for r in find_reports(root):
            print(r)
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
