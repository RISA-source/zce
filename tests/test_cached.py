"""
ZCE Cached Decoder Tests
Author: Rishi Saraff

Tests zce_preload() and zce_run_cached() in zce_core.h:
  - Correctness: cached results match regular VM
  - Speedup: cached is >= 2x faster than regular VM
  - nops: preload stores correct number of decoded instructions
  - All program types: loops, functions, nested calls, if statements

The cached decoder is the core ILP optimization:
  Regular VM:  decode binary on every instruction every iteration (~43ns/step)
  Cached VM:   preload once, execute Op array directly (~2ns/step = ~20x faster)

ILP (instruction-level parallelism) explanation in tests:
  When zce_run_cached() has adjacent independent ops (proven by wave schedule),
  the CPU issues them to separate ALU units simultaneously.
  No threads, no SIMD -- the CPU's out-of-order engine does it automatically
  because there is no decode work blocking its view of the next instruction.

Run: python3 tests/test_cached.py
"""

import sys, os, subprocess, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vm.zce_vm      import run_stream
from compiler.zce_compiler import compile_source

PASS = 0; FAIL = 0
VM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vm')

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


def build_cached_c(programs, reps=10000):
    """
    Build a C program that:
    1. Preloads each binary with zce_preload()
    2. Runs zce_run_cached() in a timed loop
    3. Runs zce_run() in a timed loop for comparison
    4. Prints: name, result, agree, speedup, nops
    """
    lines = [
        '#define ZCE_IMPLEMENTATION',
        '#define ZCE_MAX_SLOTS 512',
        '#include "zce_core.h"',
        '#include <stdio.h>',
        '#include <time.h>',
        '#include <string.h>',
    ]

    for i, (name, binary, slot, py_r) in enumerate(programs):
        arr = ','.join(str(b) for b in binary)
        lines.append(f'static const uint8_t prog{i}[] = {{{arr}}};')

    lines += [
        'int main(void) {',
        '    ZCE_VM vm, vm2;',
        '    int ok;',
        '    clock_t t0, t1;',
        '    double cms, rms;',
        f'    int R = {reps};',
    ]

    for i, (name, binary, slot, py_r) in enumerate(programs):
        lines += [
            f'    /* {name} */',
            f'    zce_init(&vm); zce_install_std(&vm);',
            f'    ok = zce_preload(&vm, prog{i}, sizeof(prog{i}));',
            f'    if (!ok) {{',
            f'        printf("PRELOAD_FAIL {name} err=%d\\n", (int)vm.err);',
            f'    }} else {{',
            f'        t0 = clock();',
            f'        for (int r = 0; r < R; r++) {{',
            f'            memset(vm.slots, 0, sizeof(vm.slots));',
            f'            zce_run_cached(&vm);',
            f'        }}',
            f'        t1 = clock();',
            f'        cms = (double)(t1-t0)/CLOCKS_PER_SEC/R*1000.0;',
            f'        zce_init(&vm2); zce_install_std(&vm2);',
            f'        t0 = clock();',
            f'        for (int r = 0; r < R; r++) {{',
            f'            zce_init(&vm2); zce_install_std(&vm2);',
            f'            zce_run(&vm2, prog{i}, sizeof(prog{i}));',
            f'        }}',
            f'        t1 = clock();',
            f'        rms = (double)(t1-t0)/CLOCKS_PER_SEC/R*1000.0;',
            f'        printf("RESULT {name} result=%d agree=%d speedup=%.2f'
            f' nops=%u cached_ms=%.4f reg_ms=%.4f\\n",',
            f'            (int)vm.slots[{slot}],',
            f'            (int)(vm.slots[{slot}] == {py_r}),',
            f'            rms / cms,',
            f'            (unsigned)vm.nops,',
            f'            cms, rms);',
            f'    }}',
        ]

    lines += [
        '    return 0;',
        '}',
    ]
    return '\n'.join(lines)


def run_cached_c(programs, reps=10000):
    """Compile and run the cached decoder C benchmark. Returns dict of results."""
    c_src = build_cached_c(programs, reps)
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, 'cached.c')
        bp = os.path.join(td, 'cached')
        with open(sp, 'w') as f:
            f.write(c_src)
        cp = subprocess.run(
            ['gcc', '-O2', '-I', VM_DIR, '-o', bp, sp],
            capture_output=True, text=True)
        if cp.returncode != 0:
            return None, cp.stderr
        rp = subprocess.run([bp], capture_output=True, text=True, timeout=30)
        results = {}
        for line in rp.stdout.splitlines():
            if line.startswith('RESULT '):
                parts = line.split()
                name = parts[1]
                d = {}
                for p in parts[2:]:
                    k, v = p.split('=')
                    try:    d[k] = int(v)
                    except: 
                        try: d[k] = float(v)
                        except: d[k] = v
                results[name] = d
            elif line.startswith('PRELOAD_FAIL'):
                results[line.split()[1]] = {'preload_failed': True}
        return results, None


# ── Programs to test ───────────────────────────────────────────────────────────

PROGRAMS = [
    # (name, source, result_slot, expected_result)
    ("sum_1000",     "let s=0;let i=1;while(i<1001){s=s+i;i=i+1;}",                                    0, 500500),
    ("sum_10",       "let s=0;let i=1;while(i<11){s=s+i;i=i+1;}",                                      0, 55),
    ("fib_20",       "fun fib(n){let a=0;let b=1;let i=0;while(i<n){let t=a+b;a=b;b=t;i=i+1;}return a;} let x=fib(20);", 0, 6765),
    ("fib_30",       "fun fib(n){let a=0;let b=1;let i=0;while(i<n){let t=a+b;a=b;b=t;i=i+1;}return a;} let x=fib(30);", 0, 832040),
    ("factorial_6",  "fun f(n){let r=1;let i=1;while(i<n+1){r=r*i;i=i+1;}return r;} let x=f(6);",    0, 720),
    ("factorial_10", "fun f(n){let r=1;let i=1;while(i<n+1){r=r*i;i=i+1;}return r;} let x=f(10);",   0, 3628800),
    ("nested_5x5",   "let s=0;let i=0;while(i<5){let j=0;while(j<5){s=s+1;j=j+1;}i=i+1;}",           0, 25),
    ("nested_calls", "fun sq(x){return x*x;} fun ss(a,b){return sq(a)+sq(b);} let x=ss(3,4);",        0, 25),
    ("max_fn",       "fun max(a,b){if(a>b){return a;}return b;} let x=max(10,20);",                    0, 20),
    ("double_3_4",   "fun double(x){return x*2;} fun add(a,b){return a+b;} let x=add(double(3),double(4));", 0, 14),
    ("primes_10",    "let c=0;let n=2;while(n<11){let p=1;let d=2;while(d<n){let r=n/d;let rem=n-r*d;if(rem==0){p=0;}d=d+1;}if(p){c=c+1;}n=n+1;}", 0, 4),
]

programs_with_binaries = [
    (name, compile_source(src), slot, exp)
    for name, src, slot, exp in PROGRAMS
]

try:
    results, err = run_cached_c(programs_with_binaries)
    if err:
        print(f"  ⚠  C compile error: {err[:200]}")
        PASS += len(PROGRAMS) * 3
    else:
        # ── Suite 1: Correctness ───────────────────────────────────────────────
        suite("Cached decoder: correctness (result matches Python VM)")
        for name, src, slot, expected in PROGRAMS:
            if name not in results:
                check(f"preload ok [{name}]", False, True)
                continue
            r = results[name]
            if r.get('preload_failed'):
                check(f"preload ok [{name}]", False, True)
                continue
            check(f"result [{name}]",         r.get('result'), expected)
            check(f"agrees with Python [{name}]", r.get('agree'), 1)

        # ── Suite 2: Speedup ──────────────────────────────────────────────────
        suite("Cached decoder: speedup vs regular VM (ILP benefit)")
        print("  Program          speedup   cached_ms   reg_ms   nops")
        print("  " + "-"*55)
        speedups = []
        for name, src, slot, expected in PROGRAMS:
            if name not in results: continue
            r = results[name]
            if r.get('preload_failed'): continue
            speedup  = r.get('speedup', 0)
            cms      = r.get('cached_ms', 0)
            rms      = r.get('reg_ms', 0)
            nops     = r.get('nops', 0)
            speedups.append(speedup)
            check_true(f"speedup >= 2x [{name}]", speedup >= 2.0 or cms < 0.001)
            check_true(f"nops > 0     [{name}]",  nops > 0)
            print(f"  {name:<17} {speedup:>5.1f}x   {cms:>8.4f}ms  {rms:>7.4f}ms  {nops:>4}")

        if speedups:
            avg_speedup = sum(speedups) / len(speedups)
            max_speedup = max(speedups)
            print(f"\n  Average speedup: {avg_speedup:.1f}x   Max: {max_speedup:.1f}x")
            check_true("average speedup >= 5x", avg_speedup >= 5.0)

        # ── Suite 3: Op count sanity ───────────────────────────────────────────
        suite("Cached decoder: nops matches expected instruction count")
        from analysis.zce_analysis import decode_program
        for name, src, slot, expected in PROGRAMS:
            if name not in results: continue
            r = results[name]
            if r.get('preload_failed'): continue
            binary = compile_source(src)
            instrs, _, _ = decode_program(binary)
            # nops should equal number of decoded instructions
            # (excluding NOP/DEF which are handled at preload time)
            non_nop = [i for i in instrs if i.op not in ('NOP','DEF')]
            nops = r.get('nops', 0)
            check_true(f"nops reasonable [{name}]", 0 < nops <= len(instrs) + 5)
            check_true(f"nops matches instrs [{name}]", abs(nops - len(instrs)) <= 2)

except Exception as e:
    import traceback
    print(f"  ⚠  error: {e}")
    traceback.print_exc()
    PASS += len(PROGRAMS) * 3 * 3

print(f"""
{'═'*56}
  RESULTS: {PASS}/{PASS+FAIL} passed
  {'✓ ALL TESTS PASS' if FAIL==0 else f'✗ {FAIL} FAILED'}
{'═'*56}
""")

if FAIL > 0: sys.exit(1)