# Gradle setup for test generation

Show the user the minimal diff and ask before editing. Match the project's style:
- version catalog (`libs.xxx`) or inline coordinates,
- Kotlin DSL (`build.gradle.kts`) or Groovy (`build.gradle`).

Prefer versions already in `gradle/libs.versions.toml`. For anything new, use the current stable release and tell the user which version you picked.

## 1. Coverage (Android module)

Kotlin DSL:

```kotlin
android {
    buildTypes {
        debug {
            enableUnitTestCoverage = true
        }
    }
    testOptions {
        unitTests {
            isReturnDefaultValues = true        // android.util.Log etc. return defaults instead of throwing
            isIncludeAndroidResources = true    // only needed for Robolectric
        }
    }
}
```

Groovy: same block, with `enableUnitTestCoverage true`, `returnDefaultValues = true` and `includeAndroidResources = true`.

- With this, recent Android Gradle Plugin versions provide the `createDebugUnitTestCoverageReport` task, which writes JaCoCo XML under `build/reports/coverage/test/debug/`.
- `run_tests.py --coverage` finds and runs that task automatically.
- On an old AGP that lacks it, the older property is `testCoverageEnabled true`. A custom `jacocoTestReport` task is then needed, and the user may prefer to upgrade AGP.
- If the project already uses **Kover**, keep it. `run_tests.py` also tries `koverXmlReport`, and `coverage.py` reads Kover's XML.

## 2. Coverage (plain JVM / Kotlin library module)

```kotlin
plugins { jacoco }
tasks.jacocoTestReport {
    dependsOn(tasks.test)
    reports { xml.required.set(true) }
}
```

## 3. Unit test dependencies

Add only what the code under test needs.

| Need | Dependency |
|---|---|
| tests at all | `junit:junit` (JUnit4, the Android default) |
| coroutines / `runTest` | `org.jetbrains.kotlinx:kotlinx-coroutines-test` (same version as the project's coroutines) |
| Kotlin mocking | `io.mockk:mockk` |
| Java mocking | `org.mockito:mockito-core` (+ `org.mockito.kotlin:mockito-kotlin` if mixing) |
| Flow assertions | `app.cash.turbine:turbine` |
| LiveData | `androidx.arch.core:core-testing` |
| Context / resources / Room / WorkManager | `org.robolectric:robolectric`, `androidx.test:core-ktx`, `androidx.test.ext:junit-ktx` |
| Room in tests | `androidx.room:room-testing` (same version as Room) |
| WorkManager | `androidx.work:work-testing` |
| Compose on JVM | `androidx.compose.ui:ui-test-junit4` + `debugImplementation androidx.compose.ui:ui-test-manifest` + Robolectric |

All of these go in `testImplementation`, except the Compose test manifest, which goes in `debugImplementation`.

## 4. Speed tips (optional, offer once)

In `gradle.properties`:

```
org.gradle.caching=true
org.gradle.parallel=true
org.gradle.configuration-cache=true
```

Only suggest configuration-cache if the build already works with it, because some plugins break under it.
