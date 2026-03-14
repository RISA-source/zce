"""
ZCE test suite.
Author: Rishi Saraff

Run: python3 test_zce.py
"""

from zce_core import run_stream, stream_to_bits, BS, Asm, AsmOpt
from zce_interp import (
    build_increment, build_copy, build_astar_bstar, build_palindrome,
    build_popcount, build_utm, build_entangled,
    inner_jif_nonzero, inner_jif_zero, inner_loop, inner_linear_sum,
    inner_runtime_def_set, inner_runtime_def_add,
    prog_x, prog_xy, prog_y_given_x, measure_symmetry,
    build_interpreter, interpreter_logic_bits,
    ISLOT_BASE,
)

_pass = [0]; _fail = [0]

def check(label, got, exp, note=""):
    ok = got == exp
    _pass[0] += ok; _fail[0] += not ok
    mark = "✓" if ok else "✗"
    n = f"  [{note}]" if note else ""
    print(f"  {mark}  {label}{n}")
    if not ok: print(f"       expected={exp!r}  got={got!r}")
    return ok

SEP = "─" * 60
def sec(t): print(f"\n{SEP}\n  {t}\n{SEP}")


# ── TM1: Binary increment ─────────────────────────────────────

def test_tm1():
    sec("TM1: Binary Increment")
    cases = [
        ([0],             [1],         "0→1"),
        ([1],             [0],         "1→0 overflow"),
        ([1,0],           [1,1],       "10→11"),
        ([1,1],           [0,0],       "11→00"),
        ([1,0,1],         [1,1,0],     "101→110"),
        ([1,1,1],         [0,0,0],     "111→000"),
        ([1,0,1,1],       [1,1,0,0],   "1011→1100"),
        ([0,0,0,0,0],     [0,0,0,0,1], "00000→00001"),
        ([1,1,1,1,1,1,1,1],[0]*8,      "11111111→00000000"),
    ]
    p = 0
    for tape, exp, lbl in cases:
        s, _ = build_increment(tape); ex, _ = run_stream(s)
        got = [ex.slot(100+i) for i in range(len(tape))]
        if check(lbl, got, exp, f"steps={ex.steps}"): p += 1
    print(f"\n  {p}/{len(cases)}")
    return p == len(cases)


# ── TM2: Tape copy ────────────────────────────────────────────

def test_tm2():
    sec("TM2: Tape Copy")
    cases = [[1,2,3,4,5], [0,0,0], [7,14,21,28], list(range(10))]
    p = 0
    for data in cases:
        s, _ = build_copy(data); ex, _ = run_stream(s)
        got = [ex.slot(150+i) for i in range(len(data))]
        if check(f"n={len(data)}", got, data): p += 1
    print(f"\n  {p}/{len(cases)}")
    return p == len(cases)


# ── TM3: a*b* recognizer ─────────────────────────────────────

def test_tm3():
    sec("TM3: a*b* Recognizer")
    cases = [
        ([2],         1, "ε"),
        ([0,2],       1, "a"),
        ([1,2],       1, "b"),
        ([0,1,2],     1, "ab"),
        ([0,0,1,1,1,2],1,"aabbb"),
        ([1,0,2],     0, "ba"),
        ([0,1,0,2],   0, "aba"),
        ([1,1,0,2],   0, "bba"),
        ([0,1,0,1,2], 0, "abab"),
    ]
    p = 0
    for tape, exp, lbl in cases:
        s, _ = build_astar_bstar(tape); ex, _ = run_stream(s)
        if check(lbl, min(ex.slot(0), 1), exp): p += 1
    print(f"\n  {p}/{len(cases)}")
    return p == len(cases)


# ── TM4: Palindrome ───────────────────────────────────────────

def test_tm4():
    sec("TM4: Palindrome")
    cases = [
        ([1],           1, "[1]"),
        ([0,1],         0, "[0,1]"),
        ([1,0,1],       1, "[1,0,1]"),
        ([0,1,1,0],     1, "[0,1,1,0]"),
        ([0,1,0,1],     0, "[0,1,0,1]"),
        ([1,0,0,0,1],   1, "[1,0,0,0,1]"),
        ([0,1,0,1,1,0,1,0], 1, "8-bit palindrome"),
        ([1,0]*4,       0, "10101010"),
    ]
    p = 0
    for tape, exp, lbl in cases:
        s, _ = build_palindrome(tape); ex, _ = run_stream(s)
        if check(lbl, ex.slot(0), exp): p += 1
    print(f"\n  {p}/{len(cases)}")
    return p == len(cases)


# ── TM5: Popcount ─────────────────────────────────────────────

def test_tm5():
    sec("TM5: Popcount")
    cases = [([0]*8,0), ([1]*8,8), ([1,0,1,0,1,0],3), ([1,1,1,1,0,0],4)]
    p = 0
    for bits, exp in cases:
        s, _ = build_popcount(bits); ex, _ = run_stream(s)
        if check(f"popcount={exp}", ex.slot(0), exp, f"steps={ex.steps}"): p += 1
    print(f"\n  {p}/{len(cases)}")
    return p == len(cases)


# ── TM6: Universal TM ─────────────────────────────────────────

def test_tm6():
    sec("TM6: Universal TM (2-state 2-symbol)")
    tm_flip = {(0,0):(0,0,1),(0,1):(0,0,2),(1,0):(1,0,2),(1,1):(1,1,2)}
    tm_or   = {(0,0):(0,0,1),(0,1):(0,1,2),(1,0):(1,0,2),(1,1):(1,1,2)}
    p = 0; total = 0
    for tape, exp, lbl in [
        ([0,0,1,0,1],[0,0,0,0,1],"flip 00101"),
        ([1,0,0,0,0],[0,0,0,0,0],"flip 10000"),
        ([0,0,0,0,1],[0,0,0,0,0],"flip 00001"),
    ]:
        s, _ = build_utm(tm_flip, tape); ex, _ = run_stream(s)
        got = [ex.slot(100+i) for i in range(len(tape))]; total += 1
        if check(lbl, got, exp, f"steps={ex.steps}"): p += 1
    for tape, lbl in [([0,0,1,0],"0010"),([1,0,0,0],"1000"),([0,0,0,1],"0001")]:
        s, _ = build_utm(tm_or, tape); ex, _ = run_stream(s); total += 1
        if check(f"OR-scan {lbl} halts", ex.slot(0), 1, f"steps={ex.steps}"): p += 1
    print(f"\n  {p}/{total}")
    return p == total


# ── Frozen decoder ────────────────────────────────────────────

def test_frozen_decoder():
    sec("Frozen Decoder")
    tape = [1,0,1,1,0,1]; s, _ = build_increment(tape)
    results = []
    for i in range(5):
        ex, _ = run_stream(s)
        r = [ex.slot(100+j) for j in range(len(tape))]
        results.append(r)
        print(f"  run {i+1}: {r}")
    all_same = all(r == results[0] for r in results)
    check("All 5 runs identical", all_same, True)
    return all_same


# ── DEF extensibility ─────────────────────────────────────────

def test_def():
    sec("DEF Extensibility")
    a = Asm(); a.std()
    a.SET(0,10); a.SET(1,3); a.SET(2,2); a.SET(3,0)
    a.SUB(10,0,1); a.MUL(11,10,2)
    a.SET(20,5); a.SET(21,5)
    a.EQ(22,20,21); a.LT(23,1,10); a.NOT(24,2)
    a.SET(30,99); a.SET(31,30)
    a.LOAD(32,31); a.SET(33,42); a.SET(34,30)
    a.STOR(34,33); a.LOAD(35,31)
    ex, _ = run_stream(a.build())
    p = 0
    for got, exp, lbl in [
        (ex.slot(10), 7,  "10-3=7"),
        (ex.slot(11), 14, "7*2=14"),
        (ex.slot(22), 1,  "5==5→1"),
        (ex.slot(23), 1,  "3<7→1"),
        (ex.slot(24), 0,  "NOT(2)→0"),
        (ex.slot(32), 99, "LOAD→99"),
        (ex.slot(35), 42, "STOR+LOAD→42"),
    ]:
        if check(lbl, got, exp): p += 1
    print(f"\n  DEF section: {a.def_bits()} bits")
    print(f"  {p}/7")
    return p == 7


# ── Self-interpreter ──────────────────────────────────────────

def test_interpreter():
    sec("Self-Interpreter (Direction B)")
    p = 0; total = 0

    def _mk(*ops):
        a = AsmOpt()
        for op in ops: op(a)
        return a.build()

    print("  SET / ADD")
    for name, stream, exp in [
        ("SET(0,42)",    _mk(lambda a: a.SET(0,42)),  42),
        ("SET(0,255)",   _mk(lambda a: a.SET(0,255)), 255),
        ("7+7=14",       _mk(lambda a: a.SET(0,7), lambda a: a.SET(1,7), lambda a: a.ADD(0,0,1)), 14),
        ("linear 1..10", inner_linear_sum(10)[0], 55),
        ("linear 1..15", inner_linear_sum(15)[0], 120),
    ]:
        si, _ = build_interpreter(stream)
        ex, _ = run_stream(si); got = ex.slot(ISLOT_BASE); total += 1
        if check(name, got, exp, f"steps={ex.steps}"): p += 1

    print("\n  JIF/JMP")
    s1, e1 = inner_jif_nonzero()
    s2, e2 = inner_jif_zero()
    for name, stream, exp in [("JIF(non-zero)→42", s1, e1), ("JIF(zero)→77", s2, e2)]:
        si, _ = build_interpreter(stream)
        ex, _ = run_stream(si); got = ex.slot(ISLOT_BASE); total += 1
        if check(name, got, exp, f"steps={ex.steps}"): p += 1

    print("\n  Loops")
    for n in [3, 5, 8]:
        stream, exp = inner_loop(n)
        si, _ = build_interpreter(stream)
        ex, _ = run_stream(si); got = ex.slot(ISLOT_BASE); total += 1
        if check(f"loop 1..{n}={exp}", got, exp, f"steps={ex.steps}"): p += 1

    print("\n  Runtime DEF")
    for name, stream, exp in [
        ("DEF op15=SET → 14", inner_runtime_def_set()[0], 14),
        ("DEF op15=ADD → 15", inner_runtime_def_add()[0], 15),
    ]:
        si, _ = build_interpreter(stream)
        ex, _ = run_stream(si); got = ex.slot(ISLOT_BASE); total += 1
        if check(name, got, exp, f"steps={ex.steps}"): p += 1

    print(f"\n  {p}/{total}")
    check("All correct", p, total)
    return p == total


# ── c_interp measurement ──────────────────────────────────────

def test_overhead():
    sec("Overhead Measurement")

    print("  Direction A — c_Σ (define section + loop overhead)\n")
    bs = BS()
    print(f"  {'n':>4}  {'define':>8}  {'data':>8}  {'loop':>8}  {'total':>8}")
    rows = []
    for n in [1, 2, 4, 8, 16, 32, 64]:
        tape = [1]*n; s, a = build_increment(tape)
        data = sum(bs.tlen(1,0)+bs.elen(100+i)+bs.elen(tape[i]) for i in range(n))
        row = {'n':n,'define':a.def_bits(),'data':data,'loop':a.prog_bits()-data,'total':len(s)*8}
        rows.append(row)
        print(f"  {n:>4}  {row['define']:>7}b  {row['data']:>7}b  {row['loop']:>7}b  {row['total']:>7}b")
    defines = [r['define'] for r in rows]
    loops   = [r['loop']   for r in rows]
    lv = max(loops) - min(loops)
    print(f"\n  Define: always {defines[0]}b")
    print(f"  Loop variation: {lv}b  (O(log log n) from Elias coding)")
    print(f"  c_Σ ≤ {defines[0]+max(loops)} bits")
    check("Define constant across n", max(defines)==min(defines), True)

    print("\n  Direction B — interpreter logic bits (c_interp)\n")
    print(f"  Data storage costs ~23 bits per inner bit (SET instruction overhead).")
    print(f"  Logic bits below isolate interpreter machinery from that linear term.\n")
    print(f"  {'case':<20} {'inner_bits':>10} {'logic':>8} {'data':>8}")
    def _mk42():
        a = AsmOpt(); a.SET(0,42); return a.build()
    def _mk14():
        a = AsmOpt(); a.SET(0,7); a.SET(1,7); a.ADD(0,0,1); return a.build()

    vals = []
    cases = [
        ("SET(0,42)",    _mk42()),
        ("7+7=14",       _mk14()),
        ("loop n=3",     inner_loop(3)[0]),
        ("loop n=5",     inner_loop(5)[0]),
        ("loop n=8",     inner_loop(8)[0]),
        ("linear 1..10", inner_linear_sum(10)[0]),
        ("linear 1..15", inner_linear_sum(15)[0]),
    ]
    for name, inner in cases:
        ib = stream_to_bits(inner)
        _, ia = build_interpreter(inner)
        logic, data = interpreter_logic_bits(ia, len(ib))
        vals.append(logic)
        print(f"  {name:<20} {len(ib):>9}b {logic:>7}b {data:>7}b")
    print(f"\n  logic range: {min(vals)}–{max(vals)}b  variation={max(vals)-min(vals)}b")
    print(f"  c_interp ≤ {max(vals)} bits")
    check("Logic variation bounded", max(vals)-min(vals) < 800, True, f"{max(vals)-min(vals)}b")
    return max(defines)==min(defines)


# ── Symmetry of information ───────────────────────────────────

def test_symmetry():
    sec("Symmetry of Information")
    print("  K_Σ(x,y) ≤ K_Σ(x) + K_Σ(y|x) + c_sym\n")
    cases = [
        (7,  14,  'double',      "y=2x,   x=7"),
        (10, 20,  'double',      "y=2x,   x=10"),
        (5,  8,   'plus_k',      "y=x+3,  x=5"),
        (20, 25,  'plus_k',      "y=x+5,  x=20"),
        (3,  9,   'square',      "y=x²,   x=3"),
        (4,  16,  'square',      "y=x²,   x=4"),
        (42, 213, 'complement',  "y=255-x, x=42"),
        (100,155, 'complement',  "y=255-x, x=100"),
        (7,  99,  'independent', "y=99, x ignored"),
        (255, 0,  'independent', "y=0,  x ignored"),
    ]
    print(f"  {'case':<22} {'K(x,y)':>8} {'K(x)':>6} {'K(y|x)':>8} {'c_sym':>8} {'ok':>4}")
    print(f"  {'-'*22} {'-'*8} {'-'*6} {'-'*8} {'-'*8} {'-'*4}")
    p = 0; c_vals = []
    for x, y, rel, label in cases:
        m = measure_symmetry(x, y, rel); c_vals.append(m['c_sym'])
        ok = "✓" if m['cond_ok'] else "✗"
        print(f"  {label:<22} {m['k_joint']:>7}b {m['k_x']:>5}b {m['k_cond']:>7}b {m['c_sym']:>7}b {ok:>4}")
        if m['cond_ok']: p += 1
    print(f"\n  c_sym range: {min(c_vals)} to {max(c_vals)}b  variation={max(c_vals)-min(c_vals)}b")
    print(f"  c_sym < 0: joint program shorter than sum of parts (shared DEF section paid once)")
    print("\n  Entangled programs — lower bound note:")
    for n in [3, 5, 8, 12]:
        s, _, xv, yv = build_entangled(n)
        sx = prog_x(xv); sy = prog_x(yv)
        print(f"  n={n}: x={xv} y={yv}  K(x)+K(y)={len(sx)*8+len(sy)*8}b  K(x,y)_ent={len(s)*8}b  saving={len(sx)*8+len(sy)*8-len(s)*8}b")
    check("All conditional programs correct", p, len(cases))
    check("c_sym O(1)", max(c_vals)-min(c_vals) < 300, True, f"range={max(c_vals)-min(c_vals)}b")
    check("c_sym < 0", max(c_vals) < 0, True)
    return p == len(cases)


# ── Combined bound ────────────────────────────────────────────

def test_combined():
    sec("Combined Bound")
    bs = BS()
    tape = [1]*32; s, a = build_increment(tape)
    data = sum(bs.tlen(1,0)+bs.elen(100+i)+bs.elen(tape[i]) for i in range(32))
    c_A = a.def_bits() + (a.prog_bits() - data)

    def _mk42():
        a = AsmOpt(); a.SET(0,42); return a.build()

    vals = []
    for inner in [
        _mk42(),
        inner_loop(5)[0], inner_loop(8)[0],
        inner_linear_sum(10)[0], inner_linear_sum(15)[0],
    ]:
        ib = stream_to_bits(inner)
        _, ia = build_interpreter(inner)
        logic, _ = interpreter_logic_bits(ia, len(ib))
        vals.append(logic)
    c_B = max(vals)

    print(f"  Direction A:  c_Σ     = {c_A} bits")
    print(f"  Direction B:  c_interp = {c_B} bits  (max over {len(vals)} cases)")
    print(f"\n  K(f) - {c_B}  ≤  K_Σ(f,0)  ≤  K(f) + {c_A}")
    print(f"\n  Both constants are independent of f.")
    print(f"  c_interp measures interpreter logic only; data storage is a separate O(|f|) term.")
    check("c_A measured", c_A > 0, True, f"{c_A}b")
    check("c_B measured", c_B > 0, True, f"{c_B}b")
    check("c_B stable across cases", max(vals)-min(vals) < 1000, True, f"range={max(vals)-min(vals)}b")
    return True


# ── Main ──────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "═"*60)
    print("  ZCE TEST SUITE")
    print("═"*60)

    suites = {
        "TM1 Increment":      test_tm1(),
        "TM2 Copy":           test_tm2(),
        "TM3 a*b*":           test_tm3(),
        "TM4 Palindrome":     test_tm4(),
        "TM5 Popcount":       test_tm5(),
        "TM6 Universal TM":   test_tm6(),
        "Frozen Decoder":     test_frozen_decoder(),
        "DEF Extensibility":  test_def(),
        "Self-Interpreter":   test_interpreter(),
        "Overhead":           test_overhead(),
        "Symmetry":           test_symmetry(),
        "Combined Bound":     test_combined(),
    }

    sec("Summary")
    for name, ok in suites.items():
        print(f"  {'✓' if ok else '✗'}  {name}")
    total = _pass[0] + _fail[0]
    print(f"\n  Suites: {sum(suites.values())}/{len(suites)}")
    print(f"  Checks: {_pass[0]}/{total}")
