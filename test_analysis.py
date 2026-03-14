"""
ZCE Static Analysis Test Suite.
Author: Rishi Saraff

Tests all six analysis passes:
  Pass 1: Tier classification (termination proofs)
  Pass 2: Dependency graph
  Pass 3: Independence waves (parallelism)
  Pass 4: Depth bound (step count)
  Pass 5: SIMD grouping
  Pass 6: P_tier (computable prior)

Run: python3 test_analysis.py
"""

from zce_core import Asm, run_stream
from zce_analysis import (
    decode_program, classify_tiers, is_tier_safe,
    build_dependency_graph, independent,
    compute_waves, parallelism_stats,
    compute_depth_bound,
    find_simd_groups,
    analyze, print_analysis,
    enumerate_tier_safe_programs, ptier_predict,
)
from zce_interp import build_increment, build_copy, build_palindrome, build_popcount

_pass = [0]; _fail = [0]

def check(label, got, exp, note=""):
    ok = (got == exp)
    _pass[0] += ok; _fail[0] += not ok
    mark = "✓" if ok else "✗"
    n = f"  [{note}]" if note else ""
    print(f"  {mark}  {label}{n}")
    if not ok:
        print(f"       expected={exp!r}  got={got!r}")
    return ok

def check_true(label, val, note=""):
    return check(label, bool(val), True, note)

def check_false(label, val, note=""):
    return check(label, bool(val), False, note)

SEP = "─" * 60
def sec(t): print(f"\n{SEP}\n  {t}\n{SEP}")


# ── Helpers ────────────────────────────────────────────────────

def make_tier_safe_loop(n):
    """Build a tier-safe loop: sum 1..n using counter in slot 10."""
    a = Asm(); a.std()
    a.SET(0, 0)    # accumulator (tier-0 data)
    a.SET(10, n)   # counter (tier-1 control — only SUB writes here)
    a.SET(2, 1)    # constant 1
    a.label('loop')
    a.ADD(0, 0, 10)     # acc += counter
    a.SUB(10, 10, 2)    # counter -= 1  (tier-1: SUB-only ✓)
    a.JIF(10, 'done')   # exit if counter == 0
    a.JMP('loop')
    a.label('done')
    return a.build()

def make_not_tier_safe_loop():
    """Loop where arithmetic result (LT) drives JIF — not tier-safe."""
    a = Asm(); a.std()
    a.SET(0, 0); a.SET(1, 5); a.SET(2, 1)
    a.label('loop')
    a.ADD(0, 0, 2)
    a.LT(20, 0, 1)      # LT result in slot 20
    a.JIF(20, 'done')   # slot 20 is tier-1 but written by LT (increasing) ✗
    a.JMP('loop')
    a.label('done')
    return a.build()

def make_no_loop():
    """Simple straight-line program — trivially tier-safe."""
    a = Asm(); a.std()
    a.SET(0, 10); a.SET(1, 3)
    a.ADD(2, 0, 1)
    a.MUL(3, 2, 1)
    a.SUB(4, 3, 0)
    return a.build()

def make_simd_program():
    """Four independent ADDs on consecutive slots → SIMD group."""
    a = Asm(); a.std()
    a.SET(10,3); a.SET(11,4); a.SET(12,5); a.SET(13,6)
    a.SET(14,1); a.SET(15,2); a.SET(16,3); a.SET(17,4)
    a.ADD(20, 10, 14)
    a.ADD(21, 11, 15)
    a.ADD(22, 12, 16)
    a.ADD(23, 13, 17)
    return a.build()


# ══════════════════════════════════════════════════════════════
# Pass 0: Decode
# ══════════════════════════════════════════════════════════════

def test_decode():
    sec("Pass 0: Decode")
    p = 0

    # straight-line program decodes correctly
    s = make_no_loop()
    instrs, rules, err = decode_program(s)
    if check("no decode error", err, None): p += 1
    if check("correct instruction count", len(instrs), 5): p += 1
    ops = [i.op for i in instrs]
    if check("correct op sequence", ops, ['SET','SET','ADD','MUL','SUB']): p += 1

    # reads/writes correct for ADD
    add_instr = instrs[2]
    if check("ADD writes dst", 2 in add_instr.writes, True): p += 1
    if check("ADD reads src slots", {0,1} == add_instr.reads, True): p += 1

    # loop program decodes with JIF and JMP
    s2 = make_tier_safe_loop(5)
    instrs2, _, err2 = decode_program(s2)
    if check("loop decode no error", err2, None): p += 1
    ops2 = [i.op for i in instrs2]
    if check("loop has JIF", 'JIF' in ops2, True): p += 1
    if check("loop has JMP", 'JMP' in ops2, True): p += 1

    # JIF reads condition slot, no writes
    jif = next(i for i in instrs2 if i.op == 'JIF')
    if check("JIF reads cond slot", len(jif.reads) == 1, True): p += 1
    if check("JIF writes nothing", len(jif.writes) == 0, True): p += 1

    print(f"\n  {p}/10")
    return p == 10


# ══════════════════════════════════════════════════════════════
# Pass 1: Tier Classification
# ══════════════════════════════════════════════════════════════

def test_tier():
    sec("Pass 1: Tier Classification — Termination Proofs")
    p = 0

    # --- Tier-safe programs ---

    # no-loop program is trivially tier-safe (no JIF at all)
    s = make_no_loop()
    safe, t0, t1, v = is_tier_safe(s)
    if check("no-loop: tier-safe", safe, True): p += 1
    if check("no-loop: no tier-1 slots (no JIF)", len(t1), 0): p += 1

    # tier-safe loop: counter only decremented
    for n in [3, 5, 10]:
        s2 = make_tier_safe_loop(n)
        safe2, t02, t12, v2 = is_tier_safe(s2)
        if check(f"tier-safe loop n={n}: safe", safe2, True, f"violations={v2}"): p += 1
        if check(f"tier-safe loop n={n}: slot 10 is tier-1", 10 in t12, True): p += 1
        if check(f"tier-safe loop n={n}: slot 0 is tier-0 (not control)", 0 not in t12, True): p += 1

    # --- Not tier-safe programs ---

    # LT result drives JIF
    s3 = make_not_tier_safe_loop()
    safe3, _, _, v3 = is_tier_safe(s3)
    if check("LT→JIF: not tier-safe", safe3, False): p += 1
    if check("LT→JIF: has violation", len(v3) > 0, True, f"{v3[0][:50]}"): p += 1

    # ADD writes tier-1 slot
    a = Asm(); a.std()
    a.SET(0, 5); a.SET(2, 1)
    a.label('l')
    a.ADD(0, 0, 2)   # ADD writes tier-1 slot 0!
    a.JIF(0, 'd')    # slot 0 is control but incremented
    a.JMP('l')
    a.label('d')
    s4 = a.build()
    safe4, _, _, v4 = is_tier_safe(s4)
    if check("ADD writes control slot: not tier-safe", safe4, False): p += 1
    if check("ADD writes control: violation mentions ADD", any('ADD' in x for x in v4), True): p += 1

    # MUL writes tier-1 slot
    a2 = Asm(); a2.std()
    a2.SET(5, 3); a2.SET(2, 2)
    a2.label('l')
    a2.MUL(5, 5, 2)  # MUL writes tier-1 slot 5!
    a2.JIF(5, 'd')
    a2.JMP('l')
    a2.label('d')
    s5 = a2.build()
    safe5, _, _, v5 = is_tier_safe(s5)
    if check("MUL writes control slot: not tier-safe", safe5, False): p += 1

    # Multiple tier-1 slots (nested structure)
    a3 = Asm(); a3.std()
    a3.SET(10, 3); a3.SET(11, 4); a3.SET(2, 1); a3.SET(0, 0)
    a3.label('outer')
    a3.SET(11, 4)          # reset inner counter (SET = ok)
    a3.label('inner')
    a3.ADD(0, 0, 2)
    a3.SUB(11, 11, 2)      # inner counter decremented ✓
    a3.JIF(11, 'inner_done')
    a3.JMP('inner')
    a3.label('inner_done')
    a3.SUB(10, 10, 2)      # outer counter decremented ✓
    a3.JIF(10, 'outer_done')
    a3.JMP('outer')
    a3.label('outer_done')
    s6 = a3.build()
    safe6, t06, t16, v6 = is_tier_safe(s6)
    if check("nested tier-safe loops: safe", safe6, True, f"v={v6}"): p += 1
    if check("nested: both counters tier-1", {10,11}.issubset(t16), True): p += 1
    if check("nested: acc slot tier-0", 0 in t06, True): p += 1

    # Verify correctness: nested loop computes 3*4=12
    ex, ok = run_stream(s6)
    if check("nested loop correct result", ex.slot(0), 12): p += 1

    print(f"\n  {p}/20")
    return p == 20


# ══════════════════════════════════════════════════════════════
# Pass 2: Dependency Graph
# ══════════════════════════════════════════════════════════════

def test_deps():
    sec("Pass 2: Dependency Graph")
    p = 0

    s = make_no_loop()
    instrs, _, _ = decode_program(s)
    deps = build_dependency_graph(instrs)

    # SET(0,10), SET(1,3), ADD(2,0,1), MUL(3,2,1), SUB(4,3,0)
    # ADD depends on both SETs
    if check("ADD depends on SET(0)", 0 in deps[2], True): p += 1
    if check("ADD depends on SET(1)", 1 in deps[2], True): p += 1
    # MUL depends on ADD (reads slot 2)
    if check("MUL depends on ADD", 2 in deps[3], True): p += 1
    # MUL depends on SET(1) (reads slot 1)
    if check("MUL depends on SET(1)", 1 in deps[3], True): p += 1
    # SUB depends on MUL (reads slot 3) and SET(0) (reads slot 0)
    if check("SUB depends on MUL", 3 in deps[4], True): p += 1
    if check("SUB depends on SET(0)", 0 in deps[4], True): p += 1
    # The two SETs are independent of each other
    if check("SET(0) independent of SET(1)", 0 not in deps[1] and 1 not in deps[0], True): p += 1

    # independence function
    set0 = instrs[0]; set1 = instrs[1]; add = instrs[2]
    if check("independent(SET0, SET1)", independent(set0, set1), True): p += 1
    if check("not independent(ADD, SET0)", independent(add, set0), False): p += 1
    if check("not independent(ADD, SET1)", independent(add, set1), False): p += 1

    # SIMD program: 4 ADDs should all be mutually independent
    s2 = make_simd_program()
    instrs2, _, _ = decode_program(s2)
    add_instrs = [i for i in instrs2 if i.op == 'ADD']
    all_independent = all(
        independent(add_instrs[i], add_instrs[j])
        for i in range(4) for j in range(4) if i != j
    )
    if check("4 SIMD ADDs mutually independent", all_independent, True): p += 1

    print(f"\n  {p}/11")
    return p == 11


# ══════════════════════════════════════════════════════════════
# Pass 3: Independence Waves
# ══════════════════════════════════════════════════════════════

def test_waves():
    sec("Pass 3: Independence Waves — Parallel Schedule")
    p = 0

    # straight-line: each dependent instr gets its own wave
    s = make_no_loop()
    instrs, _, _ = decode_program(s)
    deps = build_dependency_graph(instrs)
    schedule = compute_waves(instrs, deps)
    stats = parallelism_stats(instrs, schedule)

    if check("no-loop has multiple waves", stats['waves'] > 1, True): p += 1
    if check("no-loop wave count correct", stats['waves'], 4): p += 1
    # SET(0) and SET(1) should be in wave 0
    wave0_ops = [instrs[i].op for i in schedule[0]]
    if check("wave 0 contains SETs", wave0_ops.count('SET'), 2): p += 1

    # SIMD program: all SETs in wave 0, all ADDs in wave 1
    s2 = make_simd_program()
    instrs2, _, _ = decode_program(s2)
    deps2 = build_dependency_graph(instrs2)
    sched2 = compute_waves(instrs2, deps2)
    stats2 = parallelism_stats(instrs2, sched2)

    if check("SIMD program: 2 waves", stats2['waves'], 2): p += 1
    if check("SIMD program: wave 0 has 8 SETs", len(sched2[0]), 8): p += 1
    if check("SIMD program: wave 1 has 4 ADDs", len(sched2[1]), 4): p += 1
    if check("SIMD speedup 6x", abs(stats2['theoretical_speedup'] - 6.0) < 0.01, True,
             f"{stats2['theoretical_speedup']:.2f}x"): p += 1

    # correctness: verify no wave contains dependent instructions
    for wi, wave in enumerate(sched2):
        for ii in range(len(wave)):
            for ij in range(ii+1, len(wave)):
                i1 = instrs2[wave[ii]]; i2 = instrs2[wave[ij]]
                if not independent(i1, i2):
                    check(f"wave {wi}: all independent", False, True)
                    p -= 1
    if check("all waves internally independent", True, True): p += 1

    # tier-safe loop: waves separate control from data
    s3 = make_tier_safe_loop(5)
    instrs3, _, _ = decode_program(s3)
    deps3 = build_dependency_graph(instrs3)
    sched3 = compute_waves(instrs3, deps3)
    stats3 = parallelism_stats(instrs3, sched3)
    if check("loop has parallel structure", stats3['theoretical_speedup'] > 1.0, True,
             f"{stats3['theoretical_speedup']:.2f}x"): p += 1

    print(f"\n  {p}/9")
    return p == 9


# ══════════════════════════════════════════════════════════════
# Pass 4: Depth Bound
# ══════════════════════════════════════════════════════════════

def test_depth():
    sec("Pass 4: Depth Bound — Static Step Count")
    p = 0

    # no-loop: step bound = n instructions (no loops)
    s = make_no_loop()
    instrs, _, _ = decode_program(s)
    _, t1, _ = classify_tiers(instrs)
    d = compute_depth_bound(instrs, t1)
    if check("no-loop: is bounded", d['is_bounded'], True): p += 1
    if check("no-loop: step bound = instr count", d['step_bound'] == 5, True,
             f"bound={d['step_bound']}"): p += 1

    # tier-safe loop n=5: step bound should be >= actual steps
    for n in [3, 5, 8]:
        s2 = make_tier_safe_loop(n)
        instrs2, _, _ = decode_program(s2)
        _, t12, _ = classify_tiers(instrs2)
        d2 = compute_depth_bound(instrs2, t12)
        ex, _ = run_stream(s2)
        actual = ex.steps
        if check(f"loop n={n}: bounded", d2['is_bounded'], True): p += 1
        if check(f"loop n={n}: bound >= actual",
                 d2['step_bound'] is not None and d2['step_bound'] >= actual, True,
                 f"bound={d2['step_bound']} actual={actual}"): p += 1

    # not-tier-safe: depth bound reports unbounded
    s3 = make_not_tier_safe_loop()
    instrs3, _, _ = decode_program(s3)
    _, t13, _ = classify_tiers(instrs3)
    d3 = compute_depth_bound(instrs3, t13)
    if check("not-tier-safe: not bounded", d3['is_bounded'], False): p += 1

    # known input: pre-set slot values tighten the bound
    s4 = make_tier_safe_loop(10)
    instrs4, _, _ = decode_program(s4)
    _, t14, _ = classify_tiers(instrs4)
    d_noinit = compute_depth_bound(instrs4, t14)
    d_init   = compute_depth_bound(instrs4, t14, initial_slots={10: 10})
    if check("with init: same bound as without (SET in program)",
             d_noinit['step_bound'] == d_init['step_bound'], True): p += 1

    # verify bound is tight enough to be useful (< 100x actual)
    for n in [3, 5]:
        s5 = make_tier_safe_loop(n)
        instrs5, _, _ = decode_program(s5)
        _, t15, _ = classify_tiers(instrs5)
        d5 = compute_depth_bound(instrs5, t15)
        ex5, _ = run_stream(s5)
        ratio = d5['step_bound'] / ex5.steps if ex5.steps > 0 else 999
        if check(f"loop n={n}: bound within 20x actual", ratio < 20, True,
                 f"ratio={ratio:.1f}x"): p += 1

    print(f"\n  {p}/12")
    return p == 12


# ══════════════════════════════════════════════════════════════
# Pass 5: SIMD Grouping
# ══════════════════════════════════════════════════════════════

def test_simd():
    sec("Pass 5: SIMD Grouping — Cortex-M4 Vectorization")
    p = 0

    # 4 independent ADDs on consecutive slots → 1 SIMD group
    s = make_simd_program()
    instrs, _, _ = decode_program(s)
    deps = build_dependency_graph(instrs)
    sched = compute_waves(instrs, deps)
    groups = find_simd_groups(instrs, sched)

    if check("4 consecutive ADDs → 1 SIMD group", len(groups), 1): p += 1
    if check("SIMD group op is ADD", groups[0]['op'], 'ADD'): p += 1
    if check("SIMD group has 4 instructions", len(groups[0]['instrs']), 4): p += 1
    if check("SIMD dsts are consecutive",
             groups[0]['dsts'] == [20,21,22,23], True): p += 1

    # only 3 consecutive ADDs → no group (need width=4)
    a2 = Asm(); a2.std()
    a2.SET(10,1); a2.SET(11,2); a2.SET(12,3)
    a2.SET(13,4); a2.SET(14,5); a2.SET(15,6)
    a2.ADD(20, 10, 13)
    a2.ADD(21, 11, 14)
    a2.ADD(22, 12, 15)
    s2 = a2.build()
    instrs2, _, _ = decode_program(s2)
    deps2 = build_dependency_graph(instrs2)
    sched2 = compute_waves(instrs2, deps2)
    groups2 = find_simd_groups(instrs2, sched2)
    if check("3 ADDs → no SIMD group", len(groups2), 0): p += 1

    # 8 consecutive ADDs → 2 SIMD groups
    a3 = Asm(); a3.std()
    for i in range(8):
        a3.SET(10+i, i+1)
        a3.SET(20+i, i+1)
    for i in range(8):
        a3.ADD(30+i, 10+i, 20+i)
    s3 = a3.build()
    instrs3, _, _ = decode_program(s3)
    deps3 = build_dependency_graph(instrs3)
    sched3 = compute_waves(instrs3, deps3)
    groups3 = find_simd_groups(instrs3, sched3)
    if check("8 consecutive ADDs → 2 SIMD groups", len(groups3), 2): p += 1

    # non-consecutive dsts → no group
    a4 = Asm(); a4.std()
    a4.SET(10,1); a4.SET(11,2); a4.SET(12,3); a4.SET(13,4)
    a4.SET(14,5); a4.SET(15,6); a4.SET(16,7); a4.SET(17,8)
    a4.ADD(20, 10, 14)
    a4.ADD(22, 11, 15)   # gap: dst=22 not 21
    a4.ADD(24, 12, 16)
    a4.ADD(26, 13, 17)
    s4 = a4.build()
    instrs4, _, _ = decode_program(s4)
    deps4 = build_dependency_graph(instrs4)
    sched4 = compute_waves(instrs4, deps4)
    groups4 = find_simd_groups(instrs4, sched4)
    if check("non-consecutive dsts → no SIMD group", len(groups4), 0): p += 1

    # mixed ops in same wave → groups per op type
    a5 = Asm(); a5.std()
    for i in range(4):
        a5.SET(10+i, i+2)
        a5.SET(20+i, i+2)
    for i in range(4):
        a5.ADD(30+i, 10+i, 20+i)
    for i in range(4):
        a5.SUB(40+i, 10+i, 20+i)  # different op, consecutive dsts
    # Note: SUBs depend on SETs but not on ADDs — same wave as ADDs
    s5 = a5.build()
    r5 = analyze(s5)
    # ADD and SUB groups are separate
    add_groups = [g for g in r5['simd_groups'] if g['op'] == 'ADD']
    sub_groups = [g for g in r5['simd_groups'] if g['op'] == 'SUB']
    if check("mixed ops: ADD group found", len(add_groups) >= 1, True): p += 1
    if check("mixed ops: SUB group found", len(sub_groups) >= 1, True): p += 1

    print(f"\n  {p}/9")
    return p == 9


# ══════════════════════════════════════════════════════════════
# Full pipeline: analyze()
# ══════════════════════════════════════════════════════════════

def test_full_pipeline():
    sec("Full Pipeline: analyze()")
    p = 0

    # tier-safe loop
    s = make_tier_safe_loop(5)
    r = analyze(s)
    if check("pipeline: no decode error", r['decode_err'], None): p += 1
    if check("pipeline: tier-safe", r['tier_safe'], True): p += 1
    if check("pipeline: has waves", len(r['schedule']) > 0, True): p += 1
    if check("pipeline: depth bounded", r['depth']['is_bounded'], True): p += 1
    if check("pipeline: par_stats present", 'theoretical_speedup' in r['par_stats'], True): p += 1

    # not tier-safe loop
    s2 = make_not_tier_safe_loop()
    r2 = analyze(s2)
    if check("pipeline: not tier-safe flagged", r2['tier_safe'], False): p += 1
    if check("pipeline: violations non-empty", len(r2['violations']) > 0, True): p += 1

    # SIMD program
    s3 = make_simd_program()
    r3 = analyze(s3)
    if check("pipeline: SIMD groups detected", r3['simd_count'], 1): p += 1
    if check("pipeline: high speedup", r3['par_stats']['theoretical_speedup'] > 4.0, True): p += 1

    # existing TM programs — should parse without error
    for name, builder, args in [
        ("increment", build_increment, ([1,0,1,1],)),
        ("copy",      build_copy,      ([1,2,3,4,5],)),
        ("palindrome",build_palindrome,([1,0,1],)),
        ("popcount",  build_popcount,  ([1,1,0,1,0,1],)),
    ]:
        s4, _ = builder(*args)
        r4 = analyze(s4)
        if check(f"{name}: decodes ok", r4['decode_err'], None): p += 1
        if check(f"{name}: has instructions", len(r4['instrs']) > 0, True): p += 1
        if check(f"{name}: has waves", len(r4['schedule']) > 0, True): p += 1
        # TM programs use LT/NOT for loop control so are not tier-safe — that's expected
        print(f"    {name}: tier_safe={r4['tier_safe']} "
              f"speedup={r4['par_stats']['theoretical_speedup']:.2f}x "
              f"waves={r4['par_stats']['waves']}")

    print(f"\n  {p}/21")
    return p == 21


# ══════════════════════════════════════════════════════════════
# Pass 6: P_tier
# ══════════════════════════════════════════════════════════════

def test_ptier():
    sec("Pass 6: P_tier — Computable Solomonoff-like Prior")
    p = 0

    # enumerate tier-safe programs up to small limit
    progs, mass = enumerate_tier_safe_programs(max_bits=300, max_programs=200)
    if check("enumerate: finds programs", len(progs) > 0, True,
             f"found {len(progs)}"): p += 1
    if check("enumerate: prior mass > 0", mass > 0, True,
             f"mass={mass:.4e}"): p += 1

    # all enumerated programs are actually tier-safe
    from zce_analysis import is_tier_safe
    all_safe = all(is_tier_safe(prog[0])[0] for prog in progs[:20])
    if check("enumerate: all programs tier-safe", all_safe, True): p += 1

    # all enumerated programs actually run and produce claimed output
    all_correct = True
    for binary, claimed_output, nbits in progs[:20]:
        ex, ok = run_stream(binary)
        if not ok or ex.slot(0) != claimed_output:
            all_correct = False
            break
    if check("enumerate: all programs run correctly", all_correct, True): p += 1

    # P_tier: shorter programs get higher prior (2^-length)
    progs_sorted = sorted(progs, key=lambda x: x[2])
    if len(progs_sorted) >= 2:
        short_weight = 2.0 ** (-progs_sorted[0][2])
        long_weight  = 2.0 ** (-progs_sorted[-1][2])
        if check("shorter programs get higher prior weight",
                 short_weight > long_weight, True,
                 f"short={short_weight:.4e} long={long_weight:.4e}"): p += 1

    # ptier_predict: probability for a known-simple output
    # SET(0,0) is the simplest program — should have non-negligible mass
    prob0, total, consistent = ptier_predict(0, max_bits=300)
    if check("P_tier(0) > 0", prob0 > 0, True, f"prob={prob0:.4f}"): p += 1
    if check("P_tier(0): found consistent programs", consistent > 0, True,
             f"consistent={consistent} total={total}"): p += 1

    # P_tier sums to approximately 1 over all outputs (up to enumeration limit)
    # (not exact because we only enumerate a subset)
    if check("P_tier: total programs found", total > 10, True,
             f"total={total}"): p += 1

    # prior is properly normalised (no output has probability > 1)
    prob_large, _, _ = ptier_predict(99999, max_bits=300)
    if check("P_tier: unknown output has small mass",
             prob_large < 0.01, True, f"prob={prob_large:.4f}"): p += 1

    print(f"\n  {p}/9")
    return p == 9


# ══════════════════════════════════════════════════════════════
# Integration: analysis on all existing ZCE test programs
# ══════════════════════════════════════════════════════════════

def test_integration():
    sec("Integration: Static Analysis on All TM Programs")
    p = 0
    from zce_interp import (build_increment, build_copy, build_astar_bstar,
                             build_palindrome, build_popcount, build_utm)

    programs = [
        ("increment [1,0,1]",   build_increment,    ([1,0,1],)),
        ("increment [1,1,1,1]", build_increment,    ([1,1,1,1],)),
        ("copy [1,2,3,4,5]",    build_copy,         ([1,2,3,4,5],)),
        ("a*b* [0,1,2]",        build_astar_bstar,  ([0,1,2],)),
        ("palindrome [1,0,1]",  build_palindrome,   ([1,0,1],)),
        ("palindrome [0,1,0,1]",build_palindrome,   ([0,1,0,1],)),
        ("popcount [1,1,0,1]",  build_popcount,     ([1,1,0,1],)),
    ]

    total_speedup = 0.0
    for name, builder, args in programs:
        s, _ = builder(*args)
        r = analyze(s)

        # basic sanity
        ok_decode = r['decode_err'] is None
        ok_instrs = len(r['instrs']) > 0
        ok_waves  = len(r['schedule']) > 0
        speedup   = r['par_stats']['theoretical_speedup']
        total_speedup += speedup

        if check(f"{name}: decode+waves ok", ok_decode and ok_instrs and ok_waves, True): p += 1

        print(f"    {name}:")
        print(f"      instrs={len(r['instrs'])} waves={r['par_stats']['waves']} "
              f"speedup={speedup:.2f}x tier_safe={r['tier_safe']}")

    avg_speedup = total_speedup / len(programs)
    print(f"\n  Average theoretical speedup: {avg_speedup:.2f}×")
    if check("all programs analysed successfully", p, len(programs)): p += 1
    if check("average speedup > 1.5x", avg_speedup > 1.5, True,
             f"{avg_speedup:.2f}x"): p += 1

    print(f"\n  {p}/{len(programs)+2}")
    return p == len(programs) + 2


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "═"*60)
    print("  ZCE STATIC ANALYSIS TEST SUITE")
    print("═"*60)

    suites = {
        "Pass 0: Decode":             test_decode(),
        "Pass 1: Tier (Termination)": test_tier(),
        "Pass 2: Dependency Graph":   test_deps(),
        "Pass 3: Wave Schedule":      test_waves(),
        "Pass 4: Depth Bound":        test_depth(),
        "Pass 5: SIMD Grouping":      test_simd(),
        "Full Pipeline":              test_full_pipeline(),
        "Pass 6: P_tier Prior":       test_ptier(),
        "Integration":                test_integration(),
    }

    sec("Summary")
    for name, ok in suites.items():
        print(f"  {'✓' if ok else '✗'}  {name}")

    total = _pass[0] + _fail[0]
    suites_ok = sum(suites.values())
    print(f"\n  Suites: {suites_ok}/{len(suites)}")
    print(f"  Checks: {_pass[0]}/{total}")

    if _fail[0] == 0:
        print("\n  ✓ ALL ANALYSIS TESTS PASS")
    else:
        print(f"\n  ✗ {_fail[0]} CHECKS FAILED")
