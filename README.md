# android-testgen v2.1: a Claude Code skill that writes tests you can trust

Point it at an Android project (Kotlin, Java or mixed). It:

1. ranks the classes most worth testing,
2. writes shared fakes, then unit tests in your project's own style,
3. runs them with Gradle and fixes failures.

It keeps a test only if it compiles, passes every time, raises coverage **and catches deliberate bugs**. That last check is mutation testing: no Gradle plugin needed, and your source file is always restored afterwards.

It also:
- flags code that looks buggy instead of locking the bug into a test,
- lists classes that need a small refactor before they can be tested,
- learns your project's conventions over time in `.testgen/lessons.md`.

## Install

1. Unzip so the folder sits at:
   - macOS / Linux: `~/.claude/skills/android-testgen/`
   - Windows: `%USERPROFILE%\.claude\skills\android-testgen\`

   You should end up with `.../skills/android-testgen/SKILL.md`, `scripts/` and `references/`.
2. Requirements:
   - Claude Code,
   - Python 3.8+ (no extra packages),
   - a JDK and Android SDK that can already build the app with `./gradlew`.
3. Open your Android project folder in Claude Code.

## Use

| Command | What happens |
|---|---|
| `/android-testgen plan` | Setup check, ranked targets and fakes plan; writes nothing |
| `/android-testgen` | Full session on the top 5 classes, in batches |
| `/android-testgen --max 10` | Same, up to 10 classes |
| `/android-testgen changed` | Only code changed vs your upstream branch, plus uncommitted work |
| `/android-testgen LoginViewModel` | Just one class |
| `/android-testgen --fast` | Skip mutation testing (quicker, weaker) |
| `/android-testgen --strict` | Mutation-test every class, target score 80% |
| `/android-testgen report` | Coverage, mutation scores, history |
| `/android-testgen hook` | Install a git pre-push check that flags untested changed classes |

You can also just ask in plain words, e.g. "write tests for the payment use cases".

## What ends up in your project

- Tests in `src/test/...`, next to the code they test. Shared fakes go in a `fakes` package.
- A `.testgen/` folder:

  | File | Contents |
  |---|---|
  | `doctor.json` | setup check results |
  | `plan.json` | ranked targets |
  | `coverage-*.json` | coverage snapshots |
  | `mutation-*.json` | mutation results |
  | `log.md` | what was done |
  | `suspected-bugs.md` | code that looks wrong |
  | `lessons.md` | project conventions it has learned |

  Ignore the folder in git; you can choose to keep `lessons.md` versioned.
- Nothing is committed unless you ask, and build-file changes are always proposed first.

## The scripts (usable on their own)

```
python3 scripts/doctor.py   <project>                      # setup + learned test style
python3 scripts/scan.py     <project> [--changed-since main] [--fail-untested]
python3 scripts/run_tests.py <project> --module :app --tests com.x.FooTest [--repeat 3] [--coverage]
python3 scripts/coverage.py summary|class|snapshot|diff <project> ...
python3 scripts/check_tests.py path/to/FooTest.kt
python3 scripts/mutate.py   run <project> --file app/src/main/.../Foo.kt --tests com.x.FooTest
python3 scripts/mutate.py   restore <project>              # if a mutation run was interrupted
python3 scripts/install_hook.py <project> [--block]
```

## Tips

- On a big project, start with `plan`, then `--max 3` to judge quality before going wide.
- Allow `./gradlew` and `python3` commands in Claude Code's permissions so the loop doesn't stop for approval.
- Mutation testing re-runs your tests once per mutant (10 by default). With a warm Gradle daemon and the build cache on, that's usually a few minutes per class. Use `--fast` when you're in a hurry.
- Read `.testgen/suspected-bugs.md` after each session. These are often the most valuable output.
- If you edit or reject a generated test, say why. It goes into `lessons.md` and the next session follows it.
