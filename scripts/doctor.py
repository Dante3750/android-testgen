#!/usr/bin/env python3
"""
doctor.py - check that an Android project is ready for test generation, and learn its test style.

Usage:
  python doctor.py <project_root> [--module app] [--json]

Reports:
  * toolchain: gradlew, Gradle version (and whether `--rerun` is supported), AGP / Kotlin versions,
    JDK, Android SDK location
  * per module: Android or JVM, Kotlin or Java, coverage enabled, unitTests options,
    test libraries present vs. needed (based on what the production code uses)
  * project test style learned from existing tests: JUnit4/5, MockK/Mockito, Truth/AssertJ/Kotest,
    Turbine, naming style, existing dispatcher rule, existing fakes
  * ACTION list: what to add before generating tests (with the exact gradle snippet names)

Also writes .testgen/doctor.json, which the other scripts and the skill read.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scan  # noqa: E402

LIBS = {
    "junit4": [r"junit:junit", r"\bjunit4\b", r"\"junit\"\s*:\s*\"junit"],
    "junit5": [r"org\.junit\.jupiter", r"junit-jupiter", r"de\.mannodermaus"],
    "mockk": [r"io\.mockk"],
    "mockito": [r"org\.mockito"],
    "coroutines-test": [r"kotlinx-coroutines-test"],
    "turbine": [r"app\.cash\.turbine"],
    "truth": [r"com\.google\.truth"],
    "assertj": [r"org\.assertj"],
    "kotest": [r"io\.kotest"],
    "robolectric": [r"org\.robolectric"],
    "core-testing": [r"androidx\.arch\.core:core-testing", r"arch-core-testing", r"core-testing"],
    "room-testing": [r"room-testing"],
    "work-testing": [r"work-testing"],
    "androidx-test-core": [r"androidx\.test:core", r"androidx\.test\.ext:junit"],
    "compose-ui-test": [r"ui-test-junit4"],
}

USAGE = {  # production-code signal -> library it needs
    "coroutines-test": re.compile(r"\bsuspend\s+fun\b|viewModelScope|CoroutineScope|\bFlow<|StateFlow"),
    "turbine": re.compile(r"\bFlow<|SharedFlow|StateFlow|callbackFlow|channelFlow"),
    "core-testing": re.compile(r"\bLiveData<|MutableLiveData"),
    "room-testing": re.compile(r"@Dao\b|RoomDatabase"),
    "work-testing": re.compile(r"CoroutineWorker|ListenableWorker|: Worker\("),
}


def read(p):
    try:
        with open(p, encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) if v else ()


def gradle_version(root):
    props = read(os.path.join(root, "gradle", "wrapper", "gradle-wrapper.properties"))
    m = re.search(r"gradle-([\d.]+(?:-rc-\d+)?)-(?:bin|all)\.zip", props)
    return m.group(1) if m else None


def catalog(root):
    """Return (text, {alias: coordinate}, {version_key: version})."""
    p = os.path.join(root, "gradle", "libs.versions.toml")
    txt = read(p)
    versions = dict(re.findall(r'^\s*([\w.-]+)\s*=\s*"([^"]+)"', txt.split("[libraries]")[0], re.M)) if txt else {}
    aliases = {}
    lib_section = txt.split("[libraries]", 1)[1].split("[", 1)[0] if "[libraries]" in txt else ""
    for line in lib_section.splitlines():
        m = re.match(r'\s*([\w.-]+)\s*=\s*(.+)', line)
        if not m:
            continue
        alias, rhs = m.group(1), m.group(2)
        mod = re.search(r'module\s*=\s*"([^"]+)"', rhs)
        grp = re.search(r'group\s*=\s*"([^"]+)"', rhs)
        nm = re.search(r'name\s*=\s*"([^"]+)"', rhs)
        coord = mod.group(1) if mod else ("%s:%s" % (grp.group(1), nm.group(1)) if grp and nm else rhs.strip('" '))
        aliases[alias.replace("-", ".").replace("_", ".").lower()] = coord
    return txt, aliases, versions


def agp_kotlin_versions(root, cat_txt, versions):
    blob = cat_txt + read(os.path.join(root, "build.gradle.kts")) + read(os.path.join(root, "build.gradle")) + \
        read(os.path.join(root, "settings.gradle.kts")) + read(os.path.join(root, "settings.gradle"))
    agp = None
    for rx in (r"com\.android\.tools\.build:gradle:([\d.]+[\w-]*)", r'\bagp\s*=\s*"([^"]+)"',
               r'androidGradlePlugin\s*=\s*"([^"]+)"', r'android-?[gG]radle-?[pP]lugin\s*=\s*"([^"]+)"',
               r'id\(?\s*["\']com\.android\.(?:application|library)["\']\s*\)?\s*version\s*["\']([^"\']+)'):
        m = re.search(rx, blob)
        if m:
            agp = m.group(1)
            break
    kotlin = None
    for rx in (r'^\s*kotlin\s*=\s*"([^"]+)"', r"kotlin-gradle-plugin:([\d.]+)",
               r'(?:id\(?\s*["\']org\.jetbrains\.kotlin\.\w+["\']\s*\)?|kotlin\(["\']\w+["\']\))\s*version\s*["\']([^"\']+)'):
        m = re.search(rx, blob, re.M)
        if m:
            kotlin = m.group(1)
            break
    return agp, kotlin


def jdk():
    try:
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=20)
        m = re.search(r'version "([^"]+)"', r.stderr + r.stdout)
        return m.group(1) if m else (r.stderr.strip().splitlines() or ["unknown"])[0]
    except (OSError, subprocess.TimeoutExpired):
        return None


def sdk_dir(root):
    lp = read(os.path.join(root, "local.properties"))
    m = re.search(r"^\s*sdk\.dir\s*=\s*(.+)$", lp, re.M)
    if m:
        return m.group(1).strip().replace("\\:", ":").replace("\\\\", "\\")
    return os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")


def module_libs(build_txt, aliases):
    """Which test libs appear in this module's test configurations."""
    test_lines = [ln for ln in build_txt.splitlines()
                  if re.search(r"test(Implementation|RuntimeOnly|CompileOnly|FixturesImplementation)|"
                               r"androidTestImplementation|debugImplementation|testImplementation", ln)]
    expanded = []
    for ln in test_lines:
        expanded.append(ln)
        for ref in re.findall(r"libs\.([\w.]+)", ln):
            coord = aliases.get(ref.lower())
            if coord:
                expanded.append(coord)
        # bundles: libs.bundles.x -> can't resolve members cheaply; keep the name for matching
    blob = "\n".join(expanded)
    found = set()
    for lib, pats in LIBS.items():
        if any(re.search(p, blob, re.I) for p in pats):
            found.add(lib)
    if "junit5" not in found and re.search(r"useJUnitPlatform", build_txt):
        found.add("junit5")
    return found, blob


def test_style(mod_dir):
    counts = {k: 0 for k in ("junit4", "junit5", "mockk", "mockito", "truth", "assertj", "kotest", "turbine",
                             "robolectric", "backtick_names", "camel_names", "files")}
    fakes, rules = [], []
    for base in scan.source_sets(mod_dir, "test"):
        for f in scan.walk_sources(base):
            t = read(f)
            counts["files"] += 1
            counts["junit4"] += "import org.junit.Test" in t
            counts["junit5"] += "org.junit.jupiter" in t
            counts["mockk"] += "import io.mockk" in t
            counts["mockito"] += "org.mockito" in t
            counts["truth"] += "com.google.common.truth" in t
            counts["assertj"] += "org.assertj" in t
            counts["kotest"] += "io.kotest" in t
            counts["turbine"] += "app.cash.turbine" in t
            counts["robolectric"] += "Robolectric" in t or "AndroidJUnit4" in t
            counts["backtick_names"] += len(re.findall(r"fun\s+`", t))
            counts["camel_names"] += len(re.findall(r"@Test[\s\S]{0,80}?fun\s+[a-z]\w*\(|void\s+\w+_\w+\(", t))
            stem = os.path.splitext(os.path.basename(f))[0]
            if stem.startswith("Fake") or "/fakes/" in f.replace(os.sep, "/") or "/fake/" in f.replace(os.sep, "/"):
                fakes.append(os.path.relpath(f, mod_dir))
            if re.search(r"class\s+\w*(Dispatcher|Coroutine)\w*Rule", t):
                rules.append(os.path.relpath(f, mod_dir))
    return counts, fakes, rules


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    root = os.path.abspath(args[0])
    only = args[args.index("--module") + 1] if "--module" in args else None
    report = {"project": root, "actions": [], "modules": []}

    gw = os.path.join(root, "gradlew.bat" if os.name == "nt" else "gradlew")
    gv = gradle_version(root)
    cat_txt, aliases, versions = catalog(root)
    agp, kotlin = agp_kotlin_versions(root, cat_txt, versions)
    report.update({
        "gradlew": os.path.exists(gw), "gradle_version": gv,
        "gradle_supports_rerun": bool(gv) and version_tuple(gv) >= (7, 6),
        "agp_version": agp, "kotlin_version": kotlin, "version_catalog": bool(cat_txt),
        "jdk": jdk(), "android_sdk": sdk_dir(root),
    })
    if not report["gradlew"]:
        report["actions"].append("No gradlew at the project root. Open the folder that contains gradlew.")
    if not report["jdk"]:
        report["actions"].append("No `java` on PATH. Set JAVA_HOME to a JDK (Android Studio ships one under jbr/).")
    mods = scan.find_modules(root, only)
    if not report["android_sdk"] and any(m["android"] for m in mods):
        report["actions"].append("Android SDK not found (no sdk.dir in local.properties, no ANDROID_HOME). "
                                 "Open the project once in Android Studio or set ANDROID_HOME.")

    for mod in mods:
        build_txt = read(mod["build_file"])
        found, _ = module_libs(build_txt, aliases)
        main_txt = ""
        n_src = n_kt = 0
        for base in scan.source_sets(mod["dir"], "main"):
            for f in scan.walk_sources(base):
                n_src += 1
                n_kt += f.endswith(".kt")
                if len(main_txt) < 3_000_000:
                    main_txt += read(f)
        if n_src == 0:
            continue
        uses_kotlin = n_kt > 0 or bool(re.search(r"kotlin", build_txt, re.I))
        style, fakes, rules = test_style(mod["dir"])
        # a library imported by existing, compiling tests is on the classpath however it was declared
        for lib in ("junit4", "junit5", "mockk", "mockito", "truth", "assertj", "kotest", "turbine", "robolectric"):
            if style.get(lib):
                found.add(lib)
        if re.search(r"junit-4[\d.]*\.jar", build_txt):
            found.add("junit4")
        required = set() if ("junit4" in found or "junit5" in found) else {"junit4"}
        optional = set()
        for lib, rx in USAGE.items():
            if rx.search(main_txt):
                (required if lib in ("coroutines-test", "core-testing", "room-testing", "work-testing")
                 else optional).add(lib)
        if not ({"mockk", "mockito"} & found):
            optional.add("mockk" if uses_kotlin else "mockito")  # fakes come first; a mocking lib is a convenience
        coverage = bool(re.search(r"enableUnitTestCoverage|testCoverageEnabled|\bjacoco\b|kover", build_txt, re.I))
        entry = {
            "module": mod["gradle_path"], "android": mod["android"], "kotlin": uses_kotlin,
            "source_files": n_src, "coverage_enabled": coverage,
            "return_default_values": bool(re.search(r"(isR|r)eturnDefaultValues\s*=?\s*true", build_txt)),
            "include_android_resources": bool(re.search(r"(isI|i)ncludeAndroidResources\s*=?\s*true", build_txt)),
            "test_libs_present": sorted(found), "test_libs_missing": sorted(required - found),
            "test_libs_optional": sorted(optional - found),
            "existing_tests": style["files"], "style": style, "fakes": fakes[:30], "dispatcher_rules": rules,
        }
        report["modules"].append(entry)
        if not coverage:
            report["actions"].append("%s: enable unit-test coverage (gradle-setup.md section %s)"
                                     % (mod["gradle_path"], "1" if mod["android"] else "2"))
        if entry["test_libs_missing"]:
            report["actions"].append("%s: add test libraries: %s (gradle-setup.md section 3)"
                                     % (mod["gradle_path"], ", ".join(entry["test_libs_missing"])))
        if mod["android"] and not entry["return_default_values"]:
            report["actions"].append("%s: consider unitTests.isReturnDefaultValues = true (android.util.Log etc.)"
                                     % mod["gradle_path"])

    os.makedirs(os.path.join(root, ".testgen"), exist_ok=True)
    with open(os.path.join(root, ".testgen", "doctor.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    if "--json" in args:
        print(json.dumps(report, indent=2))
        return 0
    print("Project:  %s" % root)
    print("Gradle:   %s%s | AGP: %s | Kotlin: %s | catalog: %s" % (
        gv or "?", " (supports --rerun)" if report["gradle_supports_rerun"] else "", agp or "?", kotlin or "?",
        "yes" if cat_txt else "no"))
    print("JDK:      %s | Android SDK: %s" % (report["jdk"] or "NOT FOUND", report["android_sdk"] or "NOT FOUND"))
    for m in report["modules"]:
        s = m["style"]
        lib_pref = []
        for a, b in (("junit5", "junit4"), ("mockk", "mockito"), ("truth", "assertj")):
            if s[a] or s[b]:
                lib_pref.append(a if s[a] >= s[b] else b)
        if s["kotest"]:
            lib_pref.append("kotest")
        if s["turbine"]:
            lib_pref.append("turbine")
        naming = "backticks" if s["backtick_names"] >= s["camel_names"] and s["backtick_names"] else (
            "camelCase/underscores" if s["camel_names"] else "n/a")
        print("\n%s  (%s, %s, %d source files)" % (m["module"], "android" if m["android"] else "jvm",
                                                     "kotlin" if m["kotlin"] else "java", m["source_files"]))
        print("  coverage enabled: %s | returnDefaultValues: %s" % ("yes" if m["coverage_enabled"] else "NO",
                                                                    "yes" if m["return_default_values"] else "no"))
        print("  test libs present: %s" % (", ".join(m["test_libs_present"]) or "none"))
        if m["test_libs_missing"]:
            print("  test libs MISSING: %s" % ", ".join(m["test_libs_missing"]))
        if m.get("test_libs_optional"):
            print("  optional (nice to have): %s" % ", ".join(m["test_libs_optional"]))
        print("  existing tests: %d files | style: %s | names: %s" % (m["existing_tests"],
                                                                     ", ".join(lib_pref) or "n/a", naming))
        if m["fakes"]:
            print("  existing fakes: %s" % ", ".join(os.path.basename(f) for f in m["fakes"][:10]))
        if m["dispatcher_rules"]:
            print("  dispatcher rule: %s" % ", ".join(m["dispatcher_rules"]))
    print("\nACTIONS NEEDED:" if report["actions"] else "\nReady: no setup actions needed.")
    for a in report["actions"]:
        print("  - " + a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
