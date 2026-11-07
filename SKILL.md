---
name: android-testgen
description: Generate, run and keep only high-value unit tests for an Android project (Kotlin, Java or mixed), gated by stability, coverage and mutation testing. Use when the user asks to write tests, add test coverage, generate test cases, check test quality, or test changed code in an Android/Gradle project, or runs /android-testgen.
---

# Android test generator (v2.1)

You make an Android codebase well-tested by running batches of this loop:
**plan → shared fakes → write tests → compile/run/fix → stability → coverage → mutation → keep or drop → learn.**

A test is kept only if it:
- compiles,
- passes on every run,
- raises coverage of its target, and
- catches deliberate bugs (mutants) in the code it tests.

The scripts are in `scripts/` next to this file. Run them with `python3` (`python` on Windows), using full paths:

| Script | Purpose |
|---|---|
| `doctor.py <root>` | toolchain + per-module setup check, existing test style → `.testgen/doctor.json` |
| `scan.py <root> [--module m] [--changed-since ref] --json .testgen/plan.json` | ranked targets, constructor deps, fakes plan |
| `run_tests.py <root> --module :m --tests A --tests B [--repeat 3] [--coverage]` | compact pass/fail/flaky/compile output |
| `coverage.py class <root> <fq.Class>` / `snapshot` / `diff` / `summary` | uncovered lines, before/after |
| `check_tests.py <files...>` | static gate: no-assert, sleeps, network, no-op asserts |
| `mutate.py run <root> --file <src> --module :m --tests <fq.Test> [--max 10] [--lines a-b]` | mutation score + survivors |
| `mutate.py restore <root>` | restore the source file if a mutation run was interrupted |
| `install_hook.py <root> [--block]` | git pre-push check for untested changed classes |

Before the first test of a session, read `references/conventions.md`. Read `references/gradle-setup.md` only
when setup is missing, and `references/test-writer-prompt.md` only when using parallel writers.

## Arguments

| Argument | Meaning |
|---|---|
| *(none)* | Full session on the top targets (default 5 classes) |
| `plan` | Doctor + scan + fakes plan only; write nothing |
| `changed [ref]` | Only classes changed vs `ref` (default: the upstream branch, else `main`/`master`) plus uncommitted work |
| `<ClassName>` or a file path | Just that class |
| `--max N` | Up to N classes |
| `--fast` | Skip the mutation gate (quick pass) |
| `--strict` | Mutation gate on every class, and a target mutation score of 80% |
| `report` | Coverage summary, mutation scores and `.testgen/log.md` history |
| `hook` | Install the pre-push check (ask whether it should warn or block) |

## 0. Orient (once per session)

1. If `.testgen/mutation.lock` exists, a previous run was interrupted. Run `mutate.py restore <root>` first.
2. Run `doctor.py <root>`. Resolve the ACTIONS it lists:
   - Missing JDK or SDK → tell the user exactly what to set. You can't continue without them.
   - Missing coverage setup or test libraries → show the minimal diff from `references/gradle-setup.md`, in the
     project's own style (version catalog or inline, `.kts` or Groovy), and **ask before editing build files**.
     Use catalog versions when present; otherwise use current stable versions and say which you picked.
3. Read `.testgen/lessons.md` if it exists. Follow it: it overrides the defaults in this file and in conventions.md.
4. Note the learned style from doctor: JUnit4/5, MockK/Mockito, Truth/AssertJ/Kotest, naming style, existing
   dispatcher rule, existing fakes. **Match it.**
5. Baseline: `run_tests.py <root> --module <m> --coverage` (no `--tests` → the module's whole unit suite), then
   `coverage.py snapshot <root> baseline`. If the existing suite already fails, report the failing tests and
   continue with filtered runs only.
6. Suggest adding `.testgen/` to `.gitignore`, but keep `lessons.md` versioned if the user wants it shared.

## 1. Plan

1. Run `scan.py ... --json .testgen/plan.json` and show the top ~10 with a one-line reason each.
2. **needs-refactor** classes are not tested now. List each with its concrete refactor (inject the dispatcher, inject a clock, constructor-inject, move `Context` out) and offer to do that refactor as a separate step.
3. UI classes are excluded unless the user asks (`--include-ui`).
4. If the user is present, confirm the list; otherwise take the top N and say which.

Split the chosen classes into **batches of 3–5 classes in the same module**.

## 2. Shared fakes and test utilities (once, before the first batch)

From scan's "Fakes plan":
- For each interface used by 2 or more targets with no existing fake, write one fake in
  `<module>/src/test/.../fakes/` (pattern in conventions.md).
  - Each fake gets configurable results, an error switch, and recorded calls.
  - Extend an existing fake rather than duplicating it.
- Add a `MainDispatcherRule` in a shared test-util package only if the project has none (doctor shows existing ones) and coroutines are used.
- Compile these together with the first batch, not separately.

## 3. Batch loop

**a. Understand each class.**
- Read the class fully.
- Read only the *signatures* of its dependencies, its return/state types, and 1–2 existing tests in the module.
- Get its uncovered lines with `coverage.py class`.

**b. List behaviors per class** before writing:
- success path,
- each error path,
- each `if`/`when` branch,
- null/empty/boundary inputs,
- state sequences (Loading → Success/Error),
- retries and cancellation.

Skip trivial getters, DI modules and pure data holders.

**c. Intent check.** Compare the code with its own KDoc/Javadoc, its names and the messages it returns. Mismatches are the most valuable thing this tool finds. Examples: a doc that says "at least" but code using `>`, a cap of 100 where the doc says 50, a state flag that is never reset in a `catch`.
- If code looks wrong for its obvious intent, do **not** lock the bug in with a test.
- Add it to `.testgen/suspected-bugs.md` with: file:line, what looks wrong, what is expected and why, and the test to add once it's fixed. Leave that case out of the tests.
- If a test file covers a class with a suspected bug, put a one-line comment at the top saying which cases are deliberately left out and why.

**d. Write the tests.**
- Location: `<module>/src/test/<java|kotlin>/<same package>/<Class>Test.kt` (Java only for Java-only modules).
- If a test file already exists, append to it in its style.
- Never change production code to make a test pass.
- **Parallel writers (optional):** if a batch has 3+ classes and you can spawn subagents, give each class to its own writer using `references/test-writer-prompt.md`. Writers only write files; **only you run Gradle**, because parallel Gradle runs fight over the same build directory. Without subagents, write the tests yourself one after another.

**e. Gate the whole batch together.** One Gradle call per step, with all the batch's test classes as `--tests` filters:
1. `check_tests.py <all new/changed test files>` → fix every ERROR.
2. `run_tests.py --tests A --tests B --tests C` → fix compile errors first (they block the whole batch), then failures.
   - For each failure, decide: is the *test* wrong (fix it), or the *code* wrong (step c: record the bug, drop the assertion)?
   - Max 3 fix rounds per class, then delete what still fails and note why.
3. `run_tests.py ... --repeat 3` → fix flaky tests or delete them. Usual causes: missing `runTest`/`advanceUntilIdle`, real dispatchers, shared mutable state, ordering assumptions.
4. `run_tests.py ... --coverage`, then `coverage.py class` per target. Coverage must rise. Remove tests that add no coverage and check no distinct behavior.
5. **Mutation gate.** Skip with `--fast`. Without `--strict`, run it only for classes with real logic: kind ViewModel/UseCase/Validator/Reducer/Repository/Mapper, or complexity ≥ 5.
   - Run `mutate.py run <root> --file <src> --module <m> --tests <fq.Test> --max 10`. It mutates only lines the tests execute.
   - For each SURVIVED mutant, add or sharpen an assertion that would catch it. Typical fixes: assert the exact value instead of "not null", test the boundary (`qty == 0` and `qty == 1`), assert the call really happened or stored data really changed.
   - Then re-run on just those lines: `mutate.py run ... --lines <n,m>`. One improvement round, two with `--strict`.
   - **Before killing a survivor, ask: would the killing test assert what the docs say, but the code does differently?**
     If yes, the survivor *is* a bug signal. Don't write that assertion; add or confirm the entry in
     `suspected-bugs.md` ("confirmed by a surviving `>` → `>=` mutant"). Boundary and constant survivors
     (`<`/`<=`, `n → n+1`) next to a doc that says "at least", "at most" or "capped at" are the classic case.
   - You can often kill such a survivor *partially* without locking in the bug, by asserting only the part that is
     right. Example: `format(0)` has no minus sign, while leaving the digit formatting that looks wrong untested.
   - Some survivors are *equivalent* mutants, where the code behaves the same either way. Note them and don't chase them.
   - Target: ≥ 70% (≥ 80% with `--strict`) on the mutants that are not bug signals. Report the score either way.
   - Cost: each mutant recompiles the module. That's about 10s on a small JVM module, often 30–90s on an Android
     app module. `--max 10` keeps it to minutes per class. The script prints a real estimate after the first mutant.

**f. Log and learn.**
- Append to `.testgen/log.md`: date, class, tests kept/dropped, line % before → after, mutation score, suspected bugs.
- Append to `.testgen/lessons.md` (create it with a short header) whenever you learn something reusable about *this project*. Examples:
  - a compile error you hit twice (wrong import, missing dependency),
  - a convention you discovered (custom Result type, test base class, specific fake factory),
  - anything the user corrected or edited in a test you wrote.
- Keep lessons as short imperative bullets, and edit existing ones rather than piling up duplicates.

Keep chat updates to one line per class, e.g. `CheckoutUseCase: 11 tests, line 38% → 94%, mutation 82%, 1 suspected bug`.

## 4. Wrap up

1. Run the module's full unit suite once with `--coverage` to make sure the new tests don't interfere with existing ones.
2. Run `coverage.py snapshot <root> after` and `coverage.py diff <root> baseline after`.
3. Report:
   - classes done, test files added,
   - coverage before → after (overall and per class),
   - mutation scores,
   - suspected bugs, refactor suggestions,
   - anything skipped and why.
4. Offer the pre-push hook once (`hook`), if it isn't installed.
5. Don't commit unless asked. If asked, commit only test files and fakes: `test: add unit tests for X, Y`.

## Rules that always apply

- JVM tests (`src/test`) unless a device is truly required; never add instrumented tests unless asked.
- No `Thread.sleep`, no real network, no real clock, no real dispatchers, no shared mutable static state.
- One behavior per test, descriptive names, Arrange / Act / Assert.
- Assert outcomes (returned value, emitted state, stored data), not just that a mock was called.
- Don't chase 100%: skip generated code, DI modules, logic-free data classes, trivial delegation.
- Never leave production code mutated. If a mutation run is interrupted, run `mutate.py restore` before anything else.
- Stay within scope: don't refactor or reformat unrelated code.
