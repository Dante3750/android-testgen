# Test conventions

Match the project's existing tests first. These are the defaults when there is nothing to match.

## General

- File: `<ClassName>Test.kt` in `src/test/...` with the same package as the class under test.
- Names: Kotlin backticks — `` `load emits Error when repository throws` ``. Java: `load_repositoryThrows_emitsError`.
- Structure each test as Arrange / Act / Assert, one behavior per test.
- Build the subject in a small factory/`setUp` so each test can override one dependency.
- Use fixed, readable test data (`val user = User(id = "u1", name = "Asha")`); no randomness, no real dates.

## Coroutines (Kotlin)

Use `runTest` for every suspending test. Swap `Dispatchers.Main` with a rule:

```kotlin
@OptIn(ExperimentalCoroutinesApi::class)
class MainDispatcherRule(
    val testDispatcher: TestDispatcher = UnconfinedTestDispatcher(),
) : TestWatcher() {
    override fun starting(description: Description) = Dispatchers.setMain(testDispatcher)
    override fun finished(description: Description) = Dispatchers.resetMain()
}
```

Put it once in a shared test package (e.g. `testutil/MainDispatcherRule.kt`) and reuse it:
`@get:Rule val mainRule = MainDispatcherRule()`.

- If the class takes a `CoroutineDispatcher`, pass `mainRule.testDispatcher` (or `StandardTestDispatcher(testScheduler)`).
- Use `advanceUntilIdle()` / `advanceTimeBy(ms)` for delays, debounce and retries. Never sleep.
- Use `StandardTestDispatcher` when you need to observe intermediate states such as Loading.
  `UnconfinedTestDispatcher` runs eagerly and can skip them.

### If `kotlinx-coroutines-test` is not available

Don't block on adding it. A class that takes a `CoroutineScope` or dispatcher can be tested with
`CoroutineScope(Job() + Dispatchers.Unconfined)`: launched work runs eagerly, so state is settled when the call
returns. Use `runBlocking { }` for suspend functions. To observe an in-flight state (e.g. Loading), give the fake a
`CompletableDeferred<Unit>` gate and complete it mid-test:

```kotlin
api.gate = CompletableDeferred()
vm.placeOrder(25_000)
assertEquals(true, vm.state.value.isLoading)   // request is suspended at the gate
api.gate!!.complete(Unit)
assertEquals("A-42", vm.state.value.orderId)
```

Still suggest adding `kotlinx-coroutines-test` (it's needed for `delay`, timeouts and virtual time).

## Flow / StateFlow

Use Turbine when it is available:

```kotlin
viewModel.uiState.test {
    assertEquals(UiState.Loading, awaitItem())
    assertEquals(UiState.Success(items), awaitItem())
    cancelAndIgnoreRemainingEvents()
}
```

Without Turbine, for a `StateFlow`, assert `viewModel.uiState.value` after `advanceUntilIdle()`.

## LiveData

Use `@get:Rule val instantTask = InstantTaskExecutorRule()` (from `androidx.arch.core:core-testing`), then read `.value`.

## Assert literal values, not default objects

`assertEquals(CheckoutState(), vm.state.value)` passes even if the defaults themselves are wrong: both sides change
together. That's why a `false -> true` mutant on a default value survives. Assert the literal fields you care about
instead (`assertEquals(false, state.isLoading)`), or build the expected object with explicit arguments.

## Fakes over mocks

A `fun interface` (single abstract method) needs no fake class. Pass a lambda: `CouponValidator { 20_000L }`.


```kotlin
class FakeUserRepository : UserRepository {
    var users = mutableListOf<User>()
    var error: Throwable? = null
    val savedUsers = mutableListOf<User>()

    override suspend fun getUsers(): List<User> { error?.let { throw it }; return users.toList() }
    override suspend fun save(user: User) { error?.let { throw it }; savedUsers += user }
    override fun observeUsers(): Flow<List<User>> = flow { error?.let { throw it }; emit(users.toList()) }
}
```

- Put reusable fakes in `src/test/.../fakes/`. Reuse an existing fake if there is one, and extend it rather than duplicating it.
- Use MockK (`mockk`, `coEvery`, `coVerify`) for classes that are not interfaces, or for one-off stubs.
- Avoid `relaxed = true` unless the return values truly don't matter.
- Java-only modules: use Mockito (`when(...).thenReturn(...)`, `verify(...)`).

## ViewModel example

```kotlin
class UserListViewModelTest {
    @get:Rule val mainRule = MainDispatcherRule(StandardTestDispatcher())
    private val repo = FakeUserRepository()
    private fun viewModel() = UserListViewModel(repo)

    @Test
    fun `load emits Success with users from repository`() = runTest {
        repo.users = mutableListOf(User("u1", "Asha"))
        val vm = viewModel()

        vm.load()
        advanceUntilIdle()

        assertEquals(UiState.Success(listOf(User("u1", "Asha"))), vm.uiState.value)
    }

    @Test
    fun `load emits Error when repository throws`() = runTest {
        repo.error = IOException("offline")
        val vm = viewModel()

        vm.load()
        advanceUntilIdle()

        assertTrue(vm.uiState.value is UiState.Error)
    }
}
```

## Android framework in unit tests

- `android.util.Log`, `TextUtils` and similar return defaults when `unitTests.isReturnDefaultValues = true`. Check that this is set before relying on it.
- If the class needs a real `Context`, resources, `SharedPreferences` or `Uri`, use Robolectric:
  `@RunWith(AndroidJUnit4::class)` + `ApplicationProvider.getApplicationContext<Context>()`. This requires `unitTests.isIncludeAndroidResources = true`.
- Room DAOs: `Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).allowMainThreadQueries().build()`, under Robolectric. Close the DB in `@After`.
- WorkManager workers: `TestListenableWorkerBuilder<MyWorker>(context).build()`, under Robolectric.

## Compose UI (only when asked)

Use `createComposeRule()` with Robolectric (JVM) when possible:

```kotlin
composeRule.setContent { LoginScreen(state = LoginState(error = "Wrong password"), onLogin = {}) }
composeRule.onNodeWithText("Wrong password").assertIsDisplayed()
```

Test screens through their state and callbacks, not through a real ViewModel.

## Java tests

JUnit4 + Mockito. Name tests `method_condition_expectedResult`. Use `@Before` for setup and `assertEquals`/`assertThrows` for results.

## Writing tests that kill mutants

Surviving mutants usually mean one of the following, each with its fix:

- **Weak assertion.** `assertNotNull(result)` or `assertTrue(state is Success)` is not enough. Assert the exact value or the full state object.
- **Untested boundary.** A `<` → `<=` mutant survives. Test the boundary on both sides (`qty = 0` and `qty = 1`, `size = limit` and `size = limit + 1`).
- **Boolean shortcut.** An `&&` → `||` mutant survives. Add a case where only one side of the condition is true.
- **Unchecked side effect.** A removed `repo.save(x)` survives. Assert the fake's recorded data (`fakeRepo.saved == listOf(x)`) or `verify`/`coVerify` the call, plus the resulting state.
- **Arithmetic.** A `+` → `-` mutant survives. Use inputs where the result really differs, not zeros or ones that hide it.

Equivalent mutants (where the code behaves identically either way) can't be killed. Note them and move on.
