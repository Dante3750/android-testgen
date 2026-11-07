# Prompt for a parallel test-writer subagent

Use one writer per class, all launched together (e.g. general-purpose agents). Fill in the `{…}` parts.
Writers only write files; the main agent runs Gradle and all gates afterwards.

---

You are writing JVM unit tests for one class in an Android project at `{project_root}`.

**Target:** `{fq_class}`. Source: `{source_path}`, module `{module}`, language `{kotlin|java}`.

**Uncovered lines** (from JaCoCo): `{uncovered_lines or "no coverage data"}`

**Project test style** (match it exactly):
- Test framework: `{junit4|junit5}`
- Mocking: `{mockk|mockito}`
- Assertions: `{kotlin.test/junit asserts|truth|assertj|kotest}`
- Flow assertions: `{turbine yes/no}`
- Test naming: `{backticks|camelCase|method_condition_result}`

**Use these shared fakes and utilities** (do not redefine them):
`{list of fake classes with their file paths, MainDispatcherRule path}`

**Project lessons:** `{contents of .testgen/lessons.md, or "none"}`

**Do this:**
1. Read the target class fully. Read only the signatures of its constructor dependencies and of the types it returns or emits.
2. List the behaviors to test: success path, each error path, each branch, boundary inputs, and state sequences. Aim at the uncovered lines.
3. Write `{test_file_path}`.
   - If that file already exists, add to it in its style instead of replacing it.
   - Follow the conventions in `{skill_dir}/references/conventions.md`.
4. If the code looks wrong for its obvious intent, do not assert the buggy behavior. Instead, report it.
5. Do not modify production code or shared fakes. If you need a new fake method, describe it instead of editing the fake.
6. Do not run Gradle.

**Reply with:**
- the test file path,
- the list of test names,
- any suspected bugs (file:line, what and why),
- any fake changes you need.
