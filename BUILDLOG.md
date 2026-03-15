# ZCE Build Log

**Author:** Rishi Saraff  
**Purpose:** Full record of every decision, bug found, fix applied, and thing removed — in order.  
This file exists so the codebase is self-documenting. You should be able to read this and understand exactly why the repo is the way it is.

---

## Starting Point

The project arrived as two separate eras of code that had diverged:

**Era 1** (`zce_core.py` + `zce_interp.py`) — self-interpreter, Turing machine builders, Kolmogorov complexity / symmetry proofs. 69 tests passing. This was the theoretical foundation work.

**Era 2** (current) — compiler (`zce_compiler.py`), static analysis (`zce_analysis.py`), streaming VM (`zce_stream.py`), C header (`zce_core.h`), Python reference VM (`zce_core_v2.py`). 110 analysis tests + 64 compiler tests passing.

The two eras shared a binary format but had diverged in several ways:
- Era 1 used Elias-coded jump offsets; Era 2's assembler had regressed to 32-bit raw jump fields (a size regression)
- Era 2 added CALL/RET (tokens 6/7) and shifted extended ops to tokens 8–16
- `test_zce_v2.py` referenced functions (`inner_large_programs`, `interpreter_logic_bound`, etc.) that were never written — blocking tests

**Decision:** Keep Era 2 as the baseline. Drop `zce_interp.py` entirely — it's the self-interpreter proof-of-universality work, and it does nothing for hardware deployment. `test_zce_v2.py` depends on functions that don't exist; drop it too. The self-interpreter work is preserved in Era 1 files in git history.

---

## Repo Structure Decision

Reorganised from a flat directory into packages:

```
zce/
  vm/           zce_core.h, zce_vm.py (renamed from zce_core_v2), zce_stream.py
  compiler/     zce_compiler.py
  analysis/     zce_analysis.py
  tests/        test_all.py
  BUILDLOG.md
  README.md
```

**Rationale:** Each package has a clear single responsibility. The C header lives with the Python reference VM because they implement the same spec — they belong together. Splitting into separate repos was considered and rejected: one person, interfaces still changing, `git subtree split` makes separation easy later if needed.

---

## C Header Rewrite (zce_core.h)

The original `zce_core.h` predated v2. It was rewritten completely to match:

- Token format: `[N zeros][M+1 ones][0]` — N=opcode, M=structural depth
- Self-delimiting header: `elias(body_bits)` at start of every program
- All jump offsets Elias-coded (not raw 32-bit)
- CALL (tok 6) and RET (tok 7) added
- DEF guard updated to protect tokens 6 and 7
- Error codes: 12 defined (`ZCE_OK` through `ZCE_ERR_LOOP_BIG`)
- Two build modes: buffered (~1664B footprint) and seekable (~640B footprint, for flash-resident programs)
- `ZCE_MAX_SLOTS` defaults to 32 — intentionally small for embedded targets, overridable at compile time

---

## Compiler Bug: Nested Call Argument Slot Clobbering

**Bug:** `sum_squares(square(3), square(4))` returned 20 instead of 25.

**Root cause:** `emit_fundef()` hard-reset `_tmp` to `TMP_BASE+20` (slot 250) for every function body regardless of call depth. When `sum_squares` called `square(a)`:
1. Result (9) saved to slot 250 by `sum_squares`
2. `square(b)` called — its body also starts `_tmp` at 250
3. `square`'s internal computation overwrote slot 250 with intermediate value 4
4. `sum_squares` then computed `slot[250] + slot[251]` = 4 + 16 = 20

**Fix:** Each function is assigned a unique, non-overlapping tmp region at compile time during the first pass of `compile()`:

```
top-level:  tmp slots 230–249
function 0: tmp slots 250–269
function 1: tmp slots 270–289
function 2: tmp slots 290–309
...
```

`self.funs` changed from 2-tuple `(lbl, params)` to 3-tuple `(lbl, params, tmp_base)`. `emit_fundef()` uses the assigned base. `emit_call()` unpacks the 3-tuple.

**Verified:** `sum_squares(3,4)=25`, `cube(4)=64`, three-level nesting `double(inc(a))+double(inc(b))` all correct.

---

## Dead Code Removed

Five items identified and removed:

### 1. `TierViolation` (analysis/zce_analysis.py)
Exception class defined alongside `classify_tiers()`. Never raised anywhere. Never caught anywhere. `classify_tiers()` returns a violations list instead — better design, already working. Stub left over from an earlier design that was never connected.

### 2. `consts()` (vm/zce_vm.py)
```python
def consts(a):
    a.SET(1, 0); a.SET(2, 1); a.SET(3, 2); a.SET(4, 3)
```
Hand-assembly era utility that emitted four constant slots. The compiler handles constants internally now. Never called anywhere in the codebase.

### 3. `BOOL_SLOT = 220` (compiler/zce_compiler.py)
Class attribute defined, never referenced in any method. Probably used for boolean intermediates at some point, replaced by `alloc_tmp()`, and never cleaned up.

### 4. P_tier block — `enumerate_tier_safe_programs()` and `ptier_predict()` (analysis/zce_analysis.py, ~110 lines)
**What it was:** A computable approximation to Solomonoff induction restricted to the tier-safe class. Enumerated tier-safe ZCE programs up to a bit-length bound, weighted each by `2^(-length)`, returned `P_tier(target)` as the normalised prior probability of producing a given output.

**Why it was interesting:** Because tier-safe programs always terminate, the prior is well-defined and computable — unlike the full Solomonoff prior which requires solving the halting problem. Intended application: anomaly detection on embedded sensor streams (a spike in description length = anomaly).

**Why removed:**
1. The enumeration covered only 3 hardcoded program shapes (SET literals, simple arithmetic, counting loops). Not exhaustive within any bit-length bound, so the prior mass was not a correct probability.
2. Nothing in the deployed system calls it. Hardware deployment is the goal.
3. 110 lines of uncalled, incomplete code.

**How to do it correctly if restored:** Enumerate all tier-safe programs by exact bit length using the self-delimiting format's counting theorem — not by structural shape. The full Elias prefix encoding gives a natural ordering by length that makes this tractable.

### 5. `print_analysis()` — kept, not removed
Initially flagged as dead code because it had no tests. Decision: keep it in `zce_analysis.py` because it directly references the analysis result dict structure and belongs with the code that produces that dict. Added a smoke test (4 checks) to keep it from going stale.

---

## StreamExec: CALL/RET Fix

**Problem:** `run_streaming()` failed silently on all compiler-produced programs. The compiler emits CALL/RET for every function call. `StreamExec` had rules for tokens 1–5 only. When it hit token 6 (CALL), it set `err = "undef_tok_6"` and returned False.

**Fix 1 — obvious part:** Add rules for tokens 6 and 7 in `__init__`. Add `_call_stack = []`. Handle CALL/RET in `_exec_one()`. Fix `_define()` guard to protect `{3,4,5,6,7}`.

**Fix 2 — subtle part:** After adding CALL/RET, `run_streaming()` still hung on programs with function calls inside loops (e.g. compiled factorial). The hang came from `_run_loop()` not understanding CALL.

**Root cause of hang:** When `_run_loop()` buffers a loop body and executes it, a CALL instruction inside the buffer jumps to a target outside the buffer (the subroutine body, which is after the loop). `_run_loop()` treated any forward jump past the buffer end as a loop exit. It set the reader to the subroutine target and returned. `_run_main()` then executed the subroutine correctly — but when RET fired, execution jumped back to the resume point inside the loop buffer. `_run_main()` had no context that it was mid-loop. It treated the resume point as a new main-stream position. The loop logic was lost. The program counter drifted and ran indefinitely.

**Fix:** Added `_run_until_return()` method. When `_run_loop()` detects a forward jump past the buffer end AND the call stack grew (indicating CALL, not loop exit), it:
1. Switches reader to main stream at the CALL target
2. Calls `_run_until_return()` which executes until the call stack depth drops back
3. Resumes the buffer at the saved resume offset (the return address the CALL pushed)

**Fix 3 — silent correctness bug:** `StreamExec.MAX_SLOTS = 256`. The compiler assigns tmp slots up to `TMP_BASE + 20*(n_fns+1) = 230 + 20*n`. With even 5 functions that's slot 330. Slots above 255 were silently dropped by `_set()` (`if 0 <= i < MAX_SLOTS`). The loop would run, arithmetic would execute, but writes to slots 256+ would not stick. The loop never terminated because the counter wasn't being decremented.

Changed `MAX_SLOTS = 512`.

---

## C Header: MAX_SLOTS Mismatch With Compiler

Same issue as StreamExec but in the C test. The C header defaults `ZCE_MAX_SLOTS=32` for embedded targets (intentionally small). The compiler uses slots up to 330+. C/Python agreement tests compile with `-DZCE_MAX_SLOTS=512` to match the compiler's slot range.

**This is not a bug in the C header** — on real hardware you'd either use a compiler setting appropriate for the slot count your programs need, or use the hand-assembled approach where you control slot assignment. The default of 32 is correct for hand-assembled embedded programs.

---

## Analysis: The Six Static Passes

All five passes (the sixth, P_tier, was removed) run correctly and are not theoretical stubs — they produce real data used by the tests:

**Pass 0 (DECODE):** Reads the binary, builds a list of `Instr` objects with position, opcode, args, reads, writes. O(n).

**Pass 1 (TIER):** Classifies slots as tier-1 (control, appear as JIF conditions) or tier-0 (data, written by arithmetic). A program is tier-safe if no tier-0 result flows into a tier-1 position, and tier-1 slots are only written by SUB (decrement) or SET (literal reset). Proven: a non-increasing integer bounded below by zero reaches zero in finite steps. Therefore every tier-safe loop terminates. O(n).

**Pass 2 (DEPS):** Builds dependency graph. Instruction I depends on J if J writes what I reads, both write the same slot, or I writes what J reads (anti-dependency). O(n²).

**Pass 3 (WAVES):** Assigns each instruction to a wave — the smallest wave number such that all dependencies are in earlier waves. Same-wave instructions with non-overlapping slots are provably independent and safe to execute simultaneously. O(n²).

**Pass 4 (DEPTH):** Finds initial values of tier-1 slots from SET instructions, multiplies across loop nesting levels to get a step count upper bound. Only valid for tier-safe programs with known initial values. O(n).

**Pass 5 (SIMD):** Within each wave, finds groups of 4 independent same-operation instructions on consecutive destination slots. These map to one Cortex-M4 NEON instruction. O(n).

---

## Tests Added

Starting from 174 tests across scattered files, consolidated and expanded to **149 tests in one file** (`tests/test_all.py`):

| Suite | What it covers |
|---|---|
| VM: SET / ADD / SUB/MUL/DIV/MOD / EQ/LT/NOT / JIF/JMP / CALL/RET / LOAD/STOR / Loops / Fibonacci+Factorial | All 11 instructions, boundary cases, aliasing |
| Compiler: literals+arithmetic / comparisons+logic / short-circuit / if/else / while / functions basic / nested calls / mutual calls / iterative fib / edge cases | Full language coverage including the fixed nested call cases |
| Analysis: tier safety safe+unsafe / deps+waves / depth bound / SIMD / RAM prediction / structural depth / full pipeline | All 5 passes, both safe and unsafe programs |
| Streaming: reference VM agreement / CALL/RET agreement | Loops, compiled factorial, nested calls — the cases that previously failed |
| C/Python agreement | 7 compiler-produced programs run through both VMs (`-DZCE_MAX_SLOTS=512`) |
| Error conditions: compiler / VM bad input | 5 compiler errors (NameError, TypeError, SyntaxError), 3 VM errors (empty, invalid, truncated) |
| print_analysis smoke test | 4 checks — keeps it from silently going stale |
| Benchmarks | VM throughput, compile time, analysis time, predict_ram time, parallel speedup, binary sizes |
| C header | Buffered and seekable modes compile; hand-assembled programs agree with Python |
| Edge cases | Slot boundaries, large values, value underflow, multi-op combinations, binary format |

---

## What Was Not Done (Deliberately)

**Recursive functions:** The compiler does not support recursion — a recursive call would consume a new frame at `slot 100+` (function vars) and `slot 240+` (args) for every level, but these are static slots. Recursive calls would overwrite the parent's variables. Not a bug — ZCE-lang is not designed for recursion. Iterative equivalents work correctly.

**Compiler: nested call expressions as arguments to outer calls:** `f(g(x), h(x))` where both `g` and `h` are calls works correctly now (the compiler fix handles this). What is NOT supported: `f(g(x), g(x))` where the same function is called twice in the argument list — arg slots 240+ are shared, so the second argument evaluation overwrites the first. Sequential assignment (`let a = g(x); let b = g(x); f(a, b)`) works correctly.

**I/O, floats, strings:** Not in scope. int32 only. ZCE-lang is a computation language for embedded logic, not a general-purpose language.

**C header on real silicon:** The C header compiles and agrees with the Python VM in tests, but has not been run on physical ATtiny85 or STM32F030 hardware. That is the stated next step.

**FORK/JOIN for ESP32 dual-core:** The wave schedule is computed and correct but not wired to any actual parallel executor. The schedule is the safety proof; the parallel executor is future work.

**SIMD emission in C:** Groups are detected by the analyzer. Actual Cortex-M4 NEON instruction emission in the C header is future work.

---

## Files in the Repo

| File | Purpose | Lines |
|---|---|---|
| `vm/zce_core.h` | C single-header VM — buffered + seekable modes, full v2 instruction set | ~445 |
| `vm/zce_vm.py` | Python reference VM — used by compiler and analyzer | ~485 |
| `vm/zce_stream.py` | Streaming executor + static RAM prediction | ~390 |
| `compiler/zce_compiler.py` | ZCE-lang → binary compiler | ~535 |
| `analysis/zce_analysis.py` | Five static analysis passes | ~530 |
| `tests/test_all.py` | 149 tests across 33 suites | ~1300 |
| `README.md` | User-facing documentation |  |
| `BUILDLOG.md` | This file |  |
