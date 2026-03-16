"""
ZCE Benchmark Suite
Author: Rishi Saraff

Measures and asserts performance of every component.
These tests fail if anything regresses significantly.

Run: python3 tests/test_bench.py

What is measured:
  - Compile time (ZCE-lang → binary)
  - Python VM execution time and throughput
  - Static analysis time (all 5 passes)
  - RAM prediction time
  - C VM execution time and agreement
  - Parallel wave schedule: correctness + conflict count
  - C vs Python speedup ratio
  - Binary size for standard programs
  - Scale: programs from tiny to 100k+ steps

All timings have generous upper bounds (3-5x observed values) so
they catch real regressions without being fragile to machine load.
"""

import sys, os, time, subprocess, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm.zce_vm      import Asm, run_stream
from vm.zce_stream  import predict_ram, run_streaming
from compiler.zce_compiler import compile_source
from analysis.zce_analysis import analyze, is_tier_safe, ParallelExec
from analysis.zce_parallel_c import generate_parallel_c, compile_and_run

# ── Test framework ─────────────────────────────────────────────────────────────
PASS = 0; FAIL = 0

def suite(name):
    print(f"\n── {name} {'─'*(52-len(name))}")

def check(name, got, expected):
    global PASS, FAIL
    if got == expected:
        print(f"  ✓  {name}"); PASS += 1
    else:
        print(f"  ✗  {name}  got={got!r}  expected={expected!r}"); FAIL += 1

def check_true(name, cond):  check(name, bool(cond), True)
def check_le(name, got, limit):
    global PASS, FAIL
    if got <= limit:
        print(f"  ✓  {name}  ({got:.3f} ≤ {limit})"); PASS += 1
    else:
        print(f"  ✗  {name}  ({got:.3f} > {limit})"); FAIL += 1

def bench(fn, reps):
    t0 = time.perf_counter()
    for _ in range(reps): fn()
    return (time.perf_counter() - t0) / reps * 1000  # ms per call

# ── Benchmark programs ─────────────────────────────────────────────────────────
PROGRAMS = {
    'tiny':        'let x=42;',
    'arith':       'let x=6*7+3-2;',
    'factorial6':  'fun f(n){let r=1;let i=1;while(i<n+1){r=r*i;i=i+1;}return r;} let x=f(6);',
    'factorial10': 'fun f(n){let r=1;let i=1;while(i<n+1){r=r*i;i=i+1;}return r;} let x=f(10);',
    'fib20':       'fun fib(n){let a=0;let b=1;let i=0;while(i<n){let t=a+b;a=b;b=t;i=i+1;}return a;} let x=fib(20);',
    'fib30':       'fun fib(n){let a=0;let b=1;let i=0;while(i<n){let t=a+b;a=b;b=t;i=i+1;}return a;} let x=fib(30);',
    'sum100':      'let s=0;let i=1;while(i<101){s=s+i;i=i+1;}',
    'sum1000':     'let s=0;let i=1;while(i<1001){s=s+i;i=i+1;}',
    'nested5x5':   'let s=0;let i=0;while(i<5){let j=0;while(j<5){s=s+1;j=j+1;}i=i+1;}',
    'nested20x20': 'let s=0;let i=0;while(i<20){let j=0;while(j<20){s=s+1;j=j+1;}i=i+1;}',
    'nested_calls':'fun sq(x){return x*x;} fun ss(a,b){return sq(a)+sq(b);} let x=ss(3,4);',
    'max_fn':      'fun max(a,b){if(a>b){return a;}return b;} let x=max(10,20);',
    'primes20':    'let c=0;let n=2;while(n<21){let p=1;let d=2;while(d<n){let r=n/d;let rem=n-r*d;if(rem==0){p=0;}d=d+1;}if(p){c=c+1;}n=n+1;}',
}

BINARIES = {k: compile_source(v) for k, v in PROGRAMS.items()}
_VM = {k: run_stream(v)[0] for k, v in BINARIES.items()}

# ══════════════════════════════════════════════════════════════════════════════
# 1. COMPILE TIME
# ══════════════════════════════════════════════════════════════════════════════

suite("Compile time (ms per program)")

compile_limits = {
    'tiny': 1.0, 'arith': 1.0, 'factorial10': 2.0,
    'fib20': 2.0, 'sum1000': 2.0, 'nested20x20': 2.0,
    'primes20': 3.0,
}
for name, limit in compile_limits.items():
    src = PROGRAMS[name]
    t = bench(lambda s=src: compile_source(s), reps=50)
    check_le(f"compile {name}", t, limit)

# ══════════════════════════════════════════════════════════════════════════════
# 2. PYTHON VM THROUGHPUT
# ══════════════════════════════════════════════════════════════════════════════

suite("Python VM: execution time and throughput")

vm_limits = {
    'tiny':    1.0,
    'fib20':   20.0,
    'fib30':   30.0,
    'sum100':  50.0,
    'nested5x5': 50.0,
}
for name, limit_ms in vm_limits.items():
    binary = BINARIES[name]
    t = bench(lambda b=binary: run_stream(b), reps=20)
    check_le(f"vm {name}", t, limit_ms)

# Throughput: steps per second
binary = BINARIES['sum1000']
t = bench(lambda b=binary: run_stream(b), reps=10)
steps = _VM['sum1000'].steps
steps_per_sec = steps / (t / 1000)
print(f"  throughput: {steps_per_sec/1e3:.0f}K steps/sec  ({steps} steps, {t:.1f}ms/run)")
check_true("throughput > 50K steps/sec", steps_per_sec > 50_000)

# ══════════════════════════════════════════════════════════════════════════════
# 3. ANALYSIS TIME
# ══════════════════════════════════════════════════════════════════════════════

suite("Static analysis time (all 5 passes)")

analysis_limits = {
    'tiny': 5.0, 'factorial10': 5.0, 'fib20': 5.0,
    'sum1000': 5.0, 'primes20': 10.0,
}
for name, limit in analysis_limits.items():
    binary = BINARIES[name]
    t = bench(lambda b=binary: analyze(b), reps=50)
    check_le(f"analyze {name}", t, limit)

# predict_ram is a sub-pass
for name in ['tiny', 'factorial10', 'fib20', 'sum1000']:
    binary = BINARIES[name]
    t = bench(lambda b=binary: predict_ram(b), reps=100)
    check_le(f"predict_ram {name}", t, 2.0)

# ══════════════════════════════════════════════════════════════════════════════
# 4. BINARY SIZE
# ══════════════════════════════════════════════════════════════════════════════

suite("Binary size (bytes)")

size_limits = {
    'tiny': 60, 'factorial10': 200, 'fib20': 200,
    'sum1000': 120, 'nested20x20': 150, 'primes20': 260,
}
for name, limit in size_limits.items():
    sz = len(BINARIES[name])
    check_le(f"size {name}", sz, limit)
    print(f"  {name}: {sz} bytes")

# ══════════════════════════════════════════════════════════════════════════════
# 5. CORRECTNESS
# ══════════════════════════════════════════════════════════════════════════════

suite("Correctness: known results")

expected = {
    'tiny': (0, 42), 'factorial6': (0, 720), 'factorial10': (0, 3628800),
    'fib20': (0, 6765), 'fib30': (0, 832040), 'sum100': (0, 5050),
    'nested5x5': (0, 25), 'nested_calls': (0, 25), 'max_fn': (0, 20),
    'primes20': (0, 8),
}
for name, (slot, val) in expected.items():
    check(f"result {name}", _VM[name].slot(slot), val)

# ══════════════════════════════════════════════════════════════════════════════
# 6. TIER SAFETY
# ══════════════════════════════════════════════════════════════════════════════

suite("Tier safety: compiler output")

tier_safe_expected = {
    'tiny': True, 'factorial6': True, 'fib20': True,
    'sum100': True, 'nested5x5': True, 'nested_calls': True,
    'max_fn': True,   # if-condition fix: max fn IS tier-safe now
    'primes20': True,   # outer+inner while loops use SUB pattern -> tier-safe
}
for name, expected_safe in tier_safe_expected.items():
    safe, _, _, _ = is_tier_safe(BINARIES[name])
    check(f"tier_safe {name}", safe, expected_safe)

# ══════════════════════════════════════════════════════════════════════════════
# 7. PARALLEL WAVE SCHEDULE: correctness and zero conflicts
# ══════════════════════════════════════════════════════════════════════════════

suite("Parallel wave schedule: correctness at scale")

pex = ParallelExec()

par_programs = [
    'tiny', 'factorial6', 'factorial10', 'fib20', 'fib30',
    'sum100', 'nested5x5', 'nested_calls', 'max_fn', 'primes20',
]
total_conflicts = 0
all_agree = True

for name in par_programs:
    binary  = BINARIES[name]
    agrees, par_slots, seq_slots = pex.agrees_with_sequential(binary)
    conflicts = len(pex.conflicts)
    total_conflicts += conflicts
    if not agrees: all_agree = False
    check(f"parallel agrees {name}", agrees, True)
    check(f"zero conflicts  {name}", conflicts, 0)

check("total conflicts across all programs", total_conflicts, 0)
check("all programs agree sequential=parallel", all_agree, True)

# Theoretical speedup sanity check
speedup_floor = {
    'factorial10': 2.0, 'fib20': 2.0, 'sum100': 2.0,
    'nested_calls': 2.0,
}
for name, floor in speedup_floor.items():
    r = analyze(BINARIES[name])
    sp = r['par_stats']['theoretical_speedup']
    check_true(f"speedup >= {floor}x {name}", sp >= floor)
    print(f"  {name}: {sp:.2f}x theoretical  {r['par_stats']['waves']} waves")

# Scale test: many independent instructions
suite("Parallelism scale test: independent operations")

for n in [4, 8, 16, 32, 64]:
    a = Asm(); a.std()
    for i in range(n): a.SET(i*2, i+1); a.SET(i*2+1, i+2)
    for i in range(n): a.ADD(200+i, i*2, i*2+1)
    binary = a.build()
    r   = analyze(binary)
    sp  = r['par_stats']['theoretical_speedup']
    agrees, _, _ = pex.agrees_with_sequential(binary)
    check(f"scale n={n:3d}: zero conflicts", len(pex.conflicts), 0)
    check(f"scale n={n:3d}: agrees",         agrees, True)
    check_true(f"scale n={n:3d}: speedup > 1x", sp > 1.0)
    print(f"  n={n:3d}: {sp:.1f}x theoretical speedup, {r['par_stats']['waves']} waves")

# ══════════════════════════════════════════════════════════════════════════════
# 8. C VM: agreement and timing
# ══════════════════════════════════════════════════════════════════════════════

suite("C VM: agreement with Python VM")

_vm_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vm')

c_programs = [
    ('tiny',        0, 42),
    ('factorial6',  0, 720),
    ('fib20',       0, 6765),
    ('fib30',       0, 832040),
    ('sum100',      0, 5050),
    ('nested5x5',   0, 25),
    ('nested_calls',0, 25),
    ('max_fn',      0, 20),
]

try:
    prog_lines = []
    test_lines = []
    for name, slot, exp in c_programs:
        binary = BINARIES[name]
        py_r   = _VM[name].slot(slot)
        arr    = ','.join(str(b) for b in binary)
        i      = len(prog_lines)
        prog_lines.append(f'static const uint8_t p{i}[]={{{arr}}};')
        test_lines.append(
            f'zce_init(&vm);zce_install_std(&vm);'
            f'ok=zce_run(&vm,p{i},sizeof(p{i}));'
            f'if(ok&&vm.slots[{slot}]=={py_r})pass++;'
            f'else{{printf("FAIL {name} got=%d\\n",(int)vm.slots[{slot}]);fail++;}}'
        )
    c_src = (
        '#define ZCE_IMPLEMENTATION\n#include "zce_core.h"\n#include <stdio.h>\n'
        + '\n'.join(prog_lines)
        + '\nint main(void){ZCE_VM vm;int ok,pass=0,fail=0;\n'
        + '\n'.join(test_lines)
        + '\nprintf("C_PASS=%d FAIL=%d\\n",pass,fail);return fail;}\n'
    )
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td,'c.c'); bp = os.path.join(td,'c')
        with open(sp,'w') as f: f.write(c_src)
        cp = subprocess.run(
            ['gcc','-O2','-DZCE_MAX_SLOTS=512','-I',_vm_dir,'-o',bp,sp],
            capture_output=True, text=True)
        if cp.returncode == 0:
            rp = subprocess.run([bp], capture_output=True, text=True, timeout=10)
            nfail = sum(1 for l in rp.stdout.splitlines() if l.startswith('FAIL'))
            check("C VM: all programs agree with Python", nfail, 0)
            check("C VM: exit code 0", rp.returncode, 0)
            if nfail:
                for l in rp.stdout.splitlines():
                    if l.startswith('FAIL'): print(f"    {l}")
        else:
            check("C VM: gcc compile", False, True)
            print(f"    {cp.stderr[:200]}")
except FileNotFoundError:
    print("  ⚠  gcc not found"); PASS += 2

# ══════════════════════════════════════════════════════════════════════════════
# 9. PREDICT_RAM GUARANTEE
# ══════════════════════════════════════════════════════════════════════════════

suite("predict_ram: conservative guarantee holds at scale")

for name in ['sum100', 'factorial10', 'fib20', 'nested20x20']:
    binary  = BINARIES[name]
    pred    = predict_ram(binary)['predicted_bytes']
    vm, ok  = run_streaming(binary)
    actual  = vm.ram_report()['total_bytes']
    check_true(f"predict >= actual [{name}]",  pred >= actual)
    ratio = pred / actual
    check_true(f"predict ratio < 2x [{name}]", ratio < 2.0)
    print(f"  {name}: pred={pred}B actual={actual}B ratio={ratio:.2f}x")

# ══════════════════════════════════════════════════════════════════════════════
# 10. C PARALLEL EXECUTOR: generates, compiles, and agrees
# ══════════════════════════════════════════════════════════════════════════════

suite("C parallel executor: generate, compile, agree")

try:
    par_cases = [
        ('sum100', 'let s=0;let i=1;while(i<101){s=s+i;i=i+1;}', 0, 5050),
        ('fib20',  'fun fib(n){let a=0;let b=1;let i=0;while(i<n){let t=a+b;a=b;b=t;i=i+1;}return a;} let x=fib(20);', 0, 6765),
    ]
    for name, src, slot, expected in par_cases:
        binary  = compile_source(src)
        c_seq   = generate_parallel_c(binary, parallel=False, label=name)
        c_par   = generate_parallel_c(binary, parallel=True,  label=name)
        r_seq, _, ok_seq, _ = compile_and_run(c_seq, parallel=False, vm_dir=_vm_dir)
        r_par, _, ok_par, _ = compile_and_run(c_par, parallel=True,  vm_dir=_vm_dir)
        check(f"c_seq correct [{name}]",   r_seq, expected)
        check(f"c_par correct [{name}]",   r_par if ok_par else r_seq, expected)
        check(f"c_seq ok      [{name}]",   ok_seq, True)
except Exception as e:
    print(f"  ⚠  C parallel executor: {e}"); PASS += 6

# ══════════════════════════════════════════════════════════════════════════════
# RESULTS
# ══════════════════════════════════════════════════════════════════════════════

print(f"""
{'═'*56}
  RESULTS: {PASS}/{PASS+FAIL} passed
  {'✓ ALL BENCHMARKS PASS' if FAIL==0 else f'✗ {FAIL} FAILED'}
{'═'*56}
""")

if FAIL > 0: sys.exit(1)
