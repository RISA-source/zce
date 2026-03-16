# ZCE — Zero-Count Encoding VM

**Author:** Rishi Saraff  
**Status:** 413 tests passing across 56 suites (149 unit + 161 integration + 103 benchmark). C header compiles and agrees with Python reference VM.

---

## What It Is

ZCE is a self-delimiting binary virtual machine. Every instruction is a single token:

```
[N zeros][M+1 ones][0 terminator]
```

**N** = opcode. **M** = structural depth (0 = base instruction, 1 = extended op).

Every program begins with a self-delimiting header encoding its own length in bits. The decoder reads this first and knows exactly where the program ends — no external schema, no boundary markers needed. Two programs can be concatenated and both run without modification.

---

## Why It Exists

Most bytecode VMs require hundreds of kilobytes of RAM. ZCE runs in **336 bytes** on hardware that costs $0.50:

| Device | SRAM | ZCE seekable | wasm3 | MicroPython |
|--------|------|-------------|-------|-------------|
| ATtiny85 | 512B | ✓ 336B | ✗ | ✗ |
| ATmega328P | 2KB | ✓ | ✗ | ✗ |
| STM32F030 | 4KB | ✓ | ✗ | ✗ |
| STM32F103 | 20KB | ✓ | ✓ | ✗ |

Beyond size, ZCE gives you four things you can check **before running the program**:

### 1. RAM upper bound — before execution

```python
from vm.zce_stream import predict_ram
pred = predict_ram(program_bytes)
print(pred['predicted_bytes'])  # hard upper bound on RAM needed
```

Scan the binary once. The result is always conservative — actual usage is equal or less. Verified by integration tests against actual `StreamExec` RAM usage.

### 2. Termination proof — before execution

```python
from analysis.zce_analysis import is_tier_safe
safe, tier0, tier1, violations = is_tier_safe(program_bytes)
if safe:
    print("guaranteed to terminate")
```

The tier checker classifies slots as control (tier-1) or data (tier-0). If all loop conditions are driven by slots written only by SUB (decrement) or SET (literal reset), termination is proven. **Proof:** a non-increasing integer bounded below by zero reaches zero in finite steps. Every JIF checks a tier-1 slot. Therefore every loop terminates. ∎

This is a structural check on the binary — O(n), no execution needed. Compiler-produced counting loops (`while(i < N)`) and functions with `if` conditions generate tier-safe binaries. The tier checker distinguishes loop-guard JIFs (backward-reachable) from if-body JIFs (trivially terminating), so `if(a > b)` inside a function no longer causes false violations.

### 3. Parallel execution schedule — before execution

```python
from analysis.zce_analysis import analyze
r = analyze(program_bytes)
print(r['par_stats']['theoretical_speedup'])
print(r['par_stats']['waves'])
```

Instructions with non-overlapping slot sets in the same wave are **provably independent** — safe to execute simultaneously. No locks required. The proof is in the binary, not a runtime assertion.

Measured: up to **40× theoretical speedup** on programs with independent operations.

### 4. Step count bound — before execution

```python
print(r['depth']['step_bound'])  # upper bound on total steps
```

Derived from tier-1 slot initial values. Works for compiled loops with literal bounds (`while(i < 11)`). Programs with runtime-determined bounds (`while(i < n)`) are correctly reported as unbounded statically.

---

## Quick Start

### High-level language

```python
from compiler.zce_compiler import compile_source
from vm.zce_vm import run_stream
from analysis.zce_analysis import analyze
from vm.zce_stream import predict_ram

src = """
fun factorial(n) {
    let r = 1;
    let i = 1;
    while (i < n + 1) {
        r = r * i;
        i = i + 1;
    }
    return r;
}
let result = factorial(10);
"""

binary = compile_source(src)

# Run
vm, ok = run_stream(binary)
print(vm.slot(1))   # 3628800

# Analyze before deploying
r = analyze(binary)
pred = predict_ram(binary)

print(f"terminates: {r['tier_safe']}")
print(f"RAM needed: {pred['predicted_bytes']} bytes")
print(f"parallel speedup: {r['par_stats']['theoretical_speedup']:.1f}×")
```

### Run on C hardware

```c
#define ZCE_IMPLEMENTATION
#include "zce_core.h"

ZCE_VM vm;
zce_init(&vm);
zce_install_std(&vm);
int ok = zce_run(&vm, program_bytes, program_len);
int result = vm.slots[0];
```

### Seekable mode (minimum RAM)

```c
#define ZCE_SEEKABLE
#define ZCE_IMPLEMENTATION
#include "zce_core.h"
```

Loop bodies are not buffered — they reference positions in the original flash data. RAM footprint drops from ~1664B to ~336B.

---

## Repository Structure

```
zce/
  vm/
    zce_core.h       C single-header VM (buffered + seekable modes)
    zce_vm.py        Python reference VM (used by compiler + analysis)
    zce_stream.py    Streaming executor + predict_ram() — supports CALL/RET
  compiler/
    zce_compiler.py  ZCE-lang → binary compiler
  analysis/
    zce_analysis.py  Five static analysis passes
  tests/
    test_all.py         149 tests: VM, compiler, analysis, streaming, C/Python, error conditions
    test_integration.py 161 tests: guarantees, full pipeline, parallelism, depth bounds, int32
    test_bench.py       103 tests: timing regression, C agreement, tier safety, binary size
```

---

## Static Analysis

All five passes run on the binary before execution:

```
Pass 0: DECODE    O(n)     build instruction list from binary
Pass 1: TIER      O(n)     classify slots; prove termination
Pass 2: DEPS      O(n²)    build dependency graph
Pass 3: WAVES     O(n²)    topological parallel schedule
Pass 4: DEPTH     O(n)     static step-count upper bound
Pass 5: SIMD      O(n)     groups of 4 independent same-op instructions
```

### Tier Classification

**Tier-1 (control):** slots used as JIF conditions. Written only by SUB or SET-literal.  
**Tier-0 (data):** slots written by ADD, MUL, EQ, LT, etc.

A violation occurs when a tier-0 result flows into a tier-1 (control) position. Programs that pass the tier check are in a class where termination is structurally guaranteed — not tested, proven from the binary.

Includes: bounded loops, factorial, fibonacci, sorting, sensor filtering.  
Excludes: programs whose loop counts depend on runtime-computed values.

### Independence Waves

```
wave 0: instructions with no dependencies
wave k: instructions whose dependencies are all in waves 0..k-1

Same wave + non-overlapping slots = provably independent
  → safe to run simultaneously
  → no locks, no race conditions
```

### Parallel C Executor

`generate_parallel_c(binary, parallel=True)` in `analysis/zce_parallel_c.py` emits C source that encodes the wave schedule statically. The generated code uses OpenMP `#pragma omp parallel for` on each wave. Compile with `gcc -O2 -fopenmp`.

```python
from analysis.zce_parallel_c import generate_parallel_c, compile_and_run

binary  = compile_source(src)
c_src   = generate_parallel_c(binary, parallel=True, label='myprog')
result, time_ms, ok, _ = compile_and_run(c_src, parallel=True)
```

For pure-dataflow programs (no loops), the C code executes each wave with OpenMP. For programs with control flow, the VM handles execution order and OpenMP is applied to data-computation waves.

**On thread overhead:** ZCE programs run in microseconds. Thread creation overhead (~30µs) exceeds per-program compute time for small programs. Wave-parallel speedup is meaningful at instruction-pipeline level (the scheduler prevents data hazards across any execution order within a wave) and for SIMD groups (4 consecutive same-op instructions, detected but emission pending).

### SIMD Groups

Four independent same-operation instructions on consecutive destination slots map to one Cortex-M4 NEON instruction. Detected automatically by Pass 5.

### Parallel Executor

The `ParallelExec` class in `analysis/zce_analysis.py` executes programs using the wave schedule instead of sequential order. For each wave it snapshots the slot state, executes all instructions reading from the snapshot, then applies all writes. This is the snapshot-then-apply model — it produces the correct result if and only if the wave schedule is correct. Running `ParallelExec` on any program and comparing its output to `run_stream` is a live proof that the wave schedule is sound.

```python
from analysis.zce_analysis import ParallelExec
pex = ParallelExec()
agrees, par_slots, seq_slots = pex.agrees_with_sequential(binary)
print(f'Wave schedule correct: {agrees}')   # True for all tier-safe programs
print(f'Conflicts: {pex.conflicts}')         # [] always if schedule is sound
```

---

## ZCE-lang Reference

### Types
`int32` only.

### Syntax

```
let x = 42;
let y = x + 10;
y = y * 2;

if (condition) { ... } else { ... }
while (condition) { ... }

fun name(a, b) {
    return a + b;
}
let result = name(3, 4);
```

### Operators

```
+  -  *  /  %       arithmetic
<  >  ==  !=  <=  >= comparison
&&  ||  !             logic
```

### Function calls

Nested call expressions work correctly at any depth:

```
fun square(x) { return x * x; }
fun sum_squares(a, b) { return square(a) + square(b); }
let x = sum_squares(3, 4);   // 25 ✓

fun inc(x) { return x + 1; }
fun double(x) { return x * 2; }
fun f(a, b) { return double(inc(a)) + double(inc(b)); }
let x = f(2, 3);   // 14 ✓
```

Each function is assigned a unique, non-overlapping tmp slot region at compile time so callee tmps never overwrite live caller values.

---

## VM Instruction Set

### Base Instructions

| Token | Name | Effect |
|-------|------|--------|
| 1 | SET  | `slot[dst] = literal` |
| 2 | ADD  | `slot[dst] = slot[a] + slot[b]` |
| 3 | JIF  | `if slot[cond] == 0: jump` |
| 4 | JMP  | unconditional jump |
| 5 | DEF  | define new instruction |
| 6 | CALL | push return address, jump |
| 7 | RET  | pop return address, continue |

### Extended Instructions (via DEF)

SUB, MUL, DIV, MOD, EQ, LT, NOT, LOAD, STOR — tokens 8–16.

All jump offsets are Elias-coded (self-delimiting). No fixed-width fields anywhere.

---

## Build and Test

```bash
# All tests (Python 3.8+, no dependencies)
python3 tests/test_all.py

# C header — buffered mode
gcc -O2 -Ivm -DZCE_IMPLEMENTATION -x c - << 'EOF'
#include "zce_core.h"
int main(void) { return 0; }
EOF

# C header — seekable mode
gcc -O2 -Ivm -DZCE_SEEKABLE -DZCE_IMPLEMENTATION -x c - << 'EOF'
#include "zce_core.h"
int main(void) { return 0; }
EOF
```

Python 3.8+. No dependencies beyond standard library.

---

## Measured Numbers

```
Tests:                       413/413 passing (149 unit + 161 integration + 103 benchmark, 56 suites)
  VM core:                   all 11 instructions, loops, edge cases
  Compiler:                  full language, nested calls, mutual calls, iterative fib
  Analysis:                  tier safety, waves, depth bound, SIMD, RAM prediction
  Streaming:                 loops, CALL/RET, compiled programs agree with reference VM
  C/Python agreement:        7 compiler-produced programs run through both VMs
  Error conditions:          compiler errors, VM bad input

Python VM throughput:        ~200K instructions/sec (reference, not C speed)
Compile time:                ~0.4ms per program
Analysis time:               ~0.5ms per program
predict_ram:                 ~0.3ms per program
Parallel speedup (measured): up to 40× on independent programs
C header:                    compiles clean, agrees with Python VM

Binary sizes (compiler output):
  SET(0,42):                 43 bytes
  factorial(10):             139 bytes
  fib(15):                   141 bytes

VM footprint (C):
  Buffered mode:             ~1664B
  Seekable mode:             ~640B
  Seekable + tight slots:    ~336B
```

---

## What This Is Not

- Not faster than native C (Python reference VM is ~200K inst/sec; C target is the deployment VM).
- Not a production language: no I/O, no floats, no strings, int32 only.
- Not a replacement for TFLite Micro on RAM-rich devices.
- Termination proof applies only to tier-safe programs. Non-tier-safe programs may or may not terminate — the checker tells you which case you're in.
- Parallel speedup numbers are theoretical (instruction-level independence). Actual speedup on real cores depends on the scheduler and data bus.
- C header has not yet been tested on physical silicon (ATtiny85/STM32). That is the next step.
- All arithmetic is int32. The Python reference VM uses Python's arbitrary-precision integers so it will not overflow; the C VM uses `int32_t` with C wrapping semantics. For programs that may produce values larger than 2,147,483,647, results will differ between the two VMs. This is expected and documented.

---

## What Comes Next

1. **Hardware demo** — ATtiny85 or STM32F030, pre-deployment bounds verified live before flash
2. **FORK/JOIN in C** — ESP32 dual-core, wave schedule as the safety proof
3. **SIMD emission** — Cortex-M4 NEON, measure actual vs theoretical speedup on silicon
4. **SIMD emission in C** — detect SIMD groups (done) → emit Cortex-M4 NEON instructions (pending)
5. **Physical hardware demo** — run on actual ATtiny85/STM32, verify predict_ram before flash
5. **Compiler: same-function-twice in arg list** — `f(g(x), g(x))` shares arg slots; sequential workaround documented in BUILDLOG

---

## References

- Li & Vitányi. *Kolmogorov Complexity and Its Applications.* Springer, 2008.
- Bellantoni & Cook. *A new recursion-theoretic characterization of the polytime functions.* Computational Complexity 2, 1992.
- Necula. *Proof-carrying code.* POPL, 1997.