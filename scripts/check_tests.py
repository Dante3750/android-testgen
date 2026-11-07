#!/usr/bin/env python3
"""
check_tests.py - static quality gate for generated test files (Kotlin or Java).

Usage:
  python check_tests.py <TestFile.kt> [<TestFile2.java> ...]

Flags tests that would pass while checking nothing, or that will be flaky / slow:
  ERROR  test without any assertion or verification
  ERROR  assertTrue(true) / assertEquals(x, x) style no-op assertions
  ERROR  Thread.sleep / delay() in a test body outside runTest virtual time
  ERROR  real network (http:// or https:// URLs, OkHttp/Retrofit without MockWebServer)
  ERROR  @Ignore / @Disabled (don't ship disabled generated tests)
  WARN   reads the real clock or randomness
  WARN   GlobalScope / runBlocking in tests (prefer runTest)
  WARN   Dispatchers.setMain without resetMain (use a MainDispatcherRule)
  WARN   only verifies mock interactions, never checks a returned value or state
  WARN   println / Log in tests

Exit code: 0 clean (warnings allowed), 1 errors found.
"""
import re
import sys

ASSERT_RE = re.compile(
    r"\b(assert\w*|assertThat|verify\w*|coVerify\w*|confirmVerified|expect\w*|should\w*|"
    r"awaitItem|awaitError|awaitComplete|expectNoEvents|assertFailsWith|assertThrows|"
    r"check\(|require\(|fail\(|isEqualTo|isTrue|isFalse|containsExactly|hasSize|"
    r"onNode\w*\([^)]*\)\.assert|\.assert\w+\(|matches\(|then\()",
)
STATE_ASSERT_RE = re.compile(
    r"\b(assertEquals|assertNotEquals|assertTrue|assertFalse|assertNull|assertNotNull|assertThat|assertIs|"
    r"assertContentEquals|assertFailsWith|assertThrows|awaitItem|shouldBe|isEqualTo|expectThat|\.assert\w+\()")
NOOP_RE = re.compile(r"assert(True|That)\s*\(\s*true\s*\)|assertFalse\s*\(\s*false\s*\)|"
                     r"assertEquals\s*\(\s*(\w+)\s*,\s*\2\s*\)|assertNotNull\s*\(\s*\"")
SLEEP_RE = re.compile(r"Thread\.sleep|SystemClock\.sleep|TimeUnit\.\w+\.sleep")
NET_RE = re.compile(r"\"https?://(?!localhost|127\.0\.0\.1|example\.(com|org))")
CLOCK_RE = re.compile(r"System\.currentTimeMillis|Instant\.now\(\)|LocalDate(Time)?\.now\(\)|new Date\(\)|Random\(\)")
TEST_ANN_RE = re.compile(r"@(?:org\.junit\.)?(?:jupiter\.api\.)?Test\b|@ParameterizedTest\b")


_TOKEN_RE = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|/\*[\s\S]*?\*/|//[^\n]*')


def strip_comments(text):
    """Remove comments but keep string literals (so URLs like "https://..." survive)."""
    def repl(m):
        t = m.group(0)
        if t.startswith("/*"):
            return "\n" * t.count("\n")
        if t.startswith("//"):
            return ""
        return t
    return _TOKEN_RE.sub(repl, text)


def extract_tests(text):
    """Yield (name, line_no, body) for each @Test method, using brace matching."""
    for m in TEST_ANN_RE.finditer(text):
        start = m.end()
        # find the function/method header after the annotation
        hdr = re.compile(r"\bfun\s+(`[^`]+`|\w+)\s*\(|\bvoid\s+(\w+)\s*\(").search(text, start)
        if not hdr:
            continue
        name = (hdr.group(1) or hdr.group(2) or "?").strip("`")
        # body: either "= expr" (Kotlin expression body) or "{ ... }"
        i = text.find(")", hdr.end() - 1)
        depth_paren = 0
        j = hdr.end() - 1
        while j < len(text):
            if text[j] == "(":
                depth_paren += 1
            elif text[j] == ")":
                depth_paren -= 1
                if depth_paren == 0:
                    break
            j += 1
        i = j + 1
        k = i
        while k < len(text) and text[k] not in "{=":
            k += 1
        if k >= len(text):
            continue
        if text[k] == "=":
            # expression body such as `= runTest { ... }` - find the first brace block after it
            b = text.find("{", k)
            if b == -1:
                continue
            k = b
        depth = 0
        end = k
        while end < len(text):
            c = text[end]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        body = text[k:end + 1]
        yield name, text.count("\n", 0, m.start()) + 1, body


def check_file(path):
    try:
        raw = open(path, encoding="utf-8", errors="ignore").read()
    except OSError as e:
        return [("ERROR", 0, "cannot read file: %s" % e)], 0
    text = strip_comments(raw)
    issues = []
    tests = list(extract_tests(text))
    if not tests:
        issues.append(("ERROR", 1, "no @Test methods found"))
    has_helpers_with_asserts = bool(re.search(r"(private|internal)\s+(suspend\s+)?fun\s+\w*(assert|verify|expect)\w*",
                                              text, re.I))
    for name, line, body in tests:
        where = "%s (line %d)" % (name, line)
        if not ASSERT_RE.search(body) and not has_helpers_with_asserts:
            issues.append(("ERROR", line, "%s has no assertion or verification" % where))
        if NOOP_RE.search(body):
            issues.append(("ERROR", line, "%s contains a no-op assertion" % where))
        if SLEEP_RE.search(body):
            issues.append(("ERROR", line, "%s sleeps on the real clock; use runTest + advanceUntilIdle/advanceTimeBy"
                           % where))
        if re.search(r"\bdelay\(", body) and "runTest" not in body and "runTest" not in text:
            issues.append(("ERROR", line, "%s calls delay() outside runTest (real time)" % where))
        if ASSERT_RE.search(body) and not STATE_ASSERT_RE.search(body) and re.search(r"\b(co)?[vV]erify", body):
            issues.append(("WARN", line, "%s only verifies interactions; also assert the resulting state/value "
                           "if there is one" % where))
        if len(body.splitlines()) > 80:
            issues.append(("WARN", line, "%s is very long; split into focused tests" % where))
    if re.search(r"@Ignore\b|@Disabled\b", text):
        issues.append(("ERROR", 0, "contains @Ignore/@Disabled tests"))
    if NET_RE.search(text) and "MockWebServer" not in text and "mockwebserver" not in text:
        issues.append(("ERROR", 0, "references a real URL; tests must not touch the network"))
    if CLOCK_RE.search(text):
        issues.append(("WARN", 0, "reads the real clock or randomness; use fixed values / fake clock"))
    if "GlobalScope" in text:
        issues.append(("WARN", 0, "uses GlobalScope"))
    if re.search(r"\brunBlocking\b", text) and "kotlinx.coroutines.test" not in text:
        issues.append(("WARN", 0, "uses runBlocking; prefer runTest from kotlinx-coroutines-test"))
    if "Dispatchers.setMain" in text and "resetMain" not in text:
        issues.append(("WARN", 0, "setMain without resetMain; use a MainDispatcherRule"))
    if re.search(r"\bprintln\(|System\.out\.print|Log\.[dewiv]\(", text):
        issues.append(("WARN", 0, "prints/logs from tests"))
    names = [n for n, _, _ in tests]
    dups = {n for n in names if names.count(n) > 1}
    for d in dups:
        issues.append(("ERROR", 0, "duplicate test name %s" % d))
    return issues, len(tests)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    any_err = False
    for path in argv:
        issues, n = check_file(path)
        errs = [i for i in issues if i[0] == "ERROR"]
        warns = [i for i in issues if i[0] == "WARN"]
        any_err = any_err or bool(errs)
        print("%s: %d tests, %d errors, %d warnings" % (path, n, len(errs), len(warns)))
        for level, line, msg in errs + warns:
            print("  %s %s" % (level, msg))
    return 1 if any_err else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
