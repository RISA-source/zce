"""
ZCE self-interpreter and program builders.
Author: Rishi Saraff
"""

from zce_core import BS, Asm, AsmOpt, EXT, Exec, run_stream, stream_to_bits, consts, TOK, OP

INNER_BASE = 500   # inner program bits start here
ISLOT_BASE = 200   # inner program slots start here
RULE_BASE  = 300   # inner rule table (for DEF support): 300 + (op-6)*8
N_CALL_SITES = 17  # number of Elias decoder call sites in interpreter


# ── Direction A: TM builders ─────────────────────────────────────────────────

def build_increment(tape):
    a = Asm(); a.std(); n = len(tape)
    for i, b in enumerate(tape): a.SET(100+i, b)
    consts(a); a.SET(10, 1); a.SET(11, n-1)
    a.label('loop')
    a.LT(20, 11, 1); a.NOT(21, 20); a.JIF(21, 'done')
    a.SET(30, 100); a.ADD(30, 30, 11); a.LOAD(31, 30)
    a.MUL(32, 31, 10)
    a.ADD(33, 31, 10); a.MUL(34, 3, 32); a.SUB(33, 33, 34)
    a.STOR(30, 33)
    a.SET(10, 0); a.ADD(10, 10, 32)
    a.SUB(11, 11, 2); a.JMP('loop')
    a.label('done'); a.SET(0, 1)
    return a.build(), a

def build_copy(data):
    a = Asm(); a.std(); n = len(data)
    for i, v in enumerate(data): a.SET(100+i, v)
    consts(a); a.SET(10, 0); a.SET(11, n)
    a.label('loop')
    a.LT(20, 10, 11); a.JIF(20, 'done')
    a.SET(30, 100); a.ADD(30, 30, 10); a.LOAD(31, 30)
    a.SET(32, 150); a.ADD(32, 32, 10); a.STOR(32, 31)
    a.ADD(10, 10, 2); a.JMP('loop')
    a.label('done'); a.SET(0, 1)
    return a.build(), a

def build_astar_bstar(tape):
    a = Asm(); a.std(); n = len(tape)
    for i, s in enumerate(tape): a.SET(100+i, s)
    consts(a); a.SET(5, n); a.SET(201, 0); a.SET(10, 0); a.SET(0, 0)
    a.label('loop')
    a.LT(20, 10, 5); a.JIF(20, 'done')
    a.SET(30, 100); a.ADD(30, 30, 10); a.LOAD(31, 30)
    a.EQ(32, 31, 1); a.EQ(33, 31, 2); a.EQ(34, 31, 3)
    a.EQ(35, 201, 1); a.EQ(36, 201, 2)
    a.MUL(40, 35, 33); a.MUL(41, 36, 32)
    a.ADD(201, 201, 40); a.ADD(201, 201, 41)
    a.EQ(42, 201, 3); a.NOT(43, 42); a.MUL(44, 34, 43); a.ADD(0, 0, 44)
    a.NOT(45, 34); a.JIF(45, 'done')
    a.ADD(10, 10, 2); a.JMP('loop')
    a.label('done')
    return a.build(), a

def build_palindrome(tape):
    a = Asm(); a.std(); n = len(tape)
    for i, b in enumerate(tape): a.SET(100+i, b)
    consts(a); a.SET(10, 0); a.SET(11, n-1); a.SET(0, 1)
    a.label('loop')
    a.LT(20, 10, 11); a.JIF(20, 'done')
    a.SET(30, 100); a.ADD(30, 30, 10); a.LOAD(31, 30)
    a.SET(32, 100); a.ADD(32, 32, 11); a.LOAD(33, 32)
    a.EQ(34, 31, 33); a.NOT(35, 34)
    a.MUL(36, 0, 35); a.SUB(0, 0, 36)
    a.ADD(10, 10, 2); a.SUB(11, 11, 2); a.JMP('loop')
    a.label('done')
    return a.build(), a

def build_popcount(bits):
    a = Asm(); a.std(); n = len(bits)
    for i, b in enumerate(bits): a.SET(100+i, b)
    consts(a); a.SET(10, 0); a.SET(11, n); a.SET(0, 0)
    a.label('loop')
    a.LT(20, 10, 11); a.JIF(20, 'done')
    a.SET(30, 100); a.ADD(30, 30, 10); a.LOAD(31, 30); a.ADD(0, 0, 31)
    a.ADD(10, 10, 2); a.JMP('loop')
    a.label('done')
    return a.build(), a

def build_utm(table, tape, head=0, init_state=0):
    a = Asm(); a.std(); n = len(tape)
    for i, b in enumerate(tape): a.SET(100+i, b)
    consts(a)
    for state in range(2):
        for sym in range(2):
            ns, nb, dr = table.get((state, sym), (0, sym, 2))
            base = 300 + state*6 + sym*3
            a.SET(base, ns); a.SET(base+1, nb); a.SET(base+2, dr)
    a.SET(200, head); a.SET(201, init_state)
    a.SET(5, n); a.SET(6, 2); a.SET(7, 6); a.SET(8, 3); a.SET(9, 300); a.SET(0, 0)
    a.label('step')
    a.SET(30, 100); a.ADD(30, 30, 200); a.LOAD(31, 30)
    a.MUL(32, 201, 7); a.MUL(33, 31, 8); a.ADD(34, 32, 33); a.ADD(34, 34, 9)
    a.LOAD(36, 34)
    a.SET(35, 1); a.ADD(35, 34, 35); a.LOAD(37, 35)
    a.SET(35, 2); a.ADD(35, 34, 35); a.LOAD(38, 35)
    a.SET(30, 100); a.ADD(30, 30, 200); a.STOR(30, 37)
    a.SET(201, 0); a.ADD(201, 201, 36)
    a.EQ(40, 38, 6); a.JIF(40, 'move')
    a.SET(0, 1); a.JMP('end')
    a.label('move')
    a.ADD(41, 38, 38); a.SUB(41, 41, 2); a.ADD(200, 200, 41)
    a.LT(42, 200, 1); a.JIF(42, 'no_lo'); a.SET(200, 0)
    a.label('no_lo')
    a.LT(44, 200, 5); a.NOT(45, 44); a.JIF(45, 'no_hi')
    a.SET(35, 1); a.SUB(200, 5, 35)
    a.label('no_hi')
    a.JMP('step')
    a.label('end')
    return a.build(), a

def build_entangled(n):
    """Joint program computing x=sum(1..n) and y=sum_of_squares(1..n) in one loop."""
    a = Asm(); a.std(); consts(a)
    a.SET(10, 0); a.SET(11, n); a.SET(0, 0); a.SET(1, 0)
    a.label('loop')
    a.LT(20, 10, 11); a.JIF(20, 'done')
    a.ADD(10, 10, 2); a.ADD(0, 0, 10)
    a.MUL(30, 10, 10); a.ADD(1, 1, 30)
    a.JMP('loop')
    a.label('done')
    return a.build(), a, sum(range(1, n+1)), sum(i*i for i in range(1, n+1))


# ── Inner programs for self-interpreter testing ───────────────────────────────

def inner_jif_nonzero():
    a = AsmOpt()
    a.SET(5, 1); a.JIF(5, 'skip'); a.SET(0, 42)
    a.JMP('end'); a.label('skip'); a.SET(0, 99); a.label('end')
    return a.build(), 42

def inner_jif_zero():
    a = AsmOpt()
    a.SET(5, 0); a.JIF(5, 'taken'); a.SET(0, 99)
    a.JMP('end'); a.label('taken'); a.SET(0, 77); a.label('end')
    return a.build(), 77

def inner_loop(count):
    assert count <= 10
    a = AsmOpt(); a.SET(0, 0); a.SET(5, count)
    for i in range(count, 0, -1):
        a.label(f'step_{i}'); a.JIF(5, 'done'); a.ADD(0, 0, 5)
        if i > 1: a.SET(5, i-1)
    a.label('done')
    return a.build(), sum(range(1, count+1))

def inner_linear_sum(n):
    a = AsmOpt(); a.SET(0, 0)
    for i in range(1, n+1): a.SET(10+i, i); a.ADD(0, 0, 10+i)
    return a.build(), sum(range(1, n+1))

def inner_runtime_def_set():
    """Inner program: DEF op15=SET, op15(0,7), ADD(0,0,0) → 14."""
    body = BS()
    body.token(5,1);  body.elias(15); body.elias(1)
    body.elias(0);    body.elias(1);  body.elias(0); body.elias(1)
    body.token(15,1); body.elias(0);  body.elias(7)
    body.token(2,0);  body.elias(0);  body.elias(0); body.elias(0)
    hdr = BS(); hdr.elias(body.count())
    full = BS(); full.bits(hdr.b); full.bits(body.b)
    return full.tobytes(), 14

def inner_runtime_def_add():
    """Inner program: DEF op15=ADD, unrolled sum 1+2+3+4+5=15."""
    body = BS()
    body.token(5,1);  body.elias(15); body.elias(2)
    body.elias(0);    body.elias(0);  body.elias(0)
    body.elias(0);    body.elias(2)
    body.token(1,0);  body.elias(0);  body.elias(0)
    body.token(1,0);  body.elias(5);  body.elias(1)
    body.token(1,0);  body.elias(7);  body.elias(0)
    for _ in range(5):
        body.token(2,0);  body.elias(7); body.elias(7); body.elias(5)
        body.token(15,1); body.elias(0); body.elias(0); body.elias(7)
    hdr = BS(); hdr.elias(body.count())
    full = BS(); full.bits(hdr.b); full.bits(body.b)
    return full.tobytes(), 15


# ── Symmetry of information programs ─────────────────────────────────────────

def prog_x(x):
    a = Asm(); a.std(); a.SET(0, x); return a.build()

def prog_xy(x, y):
    a = Asm(); a.std(); a.SET(0, x); a.SET(1, y); return a.build()

def prog_y_given_x(x, y, relation):
    a = Asm(); a.std()
    if relation == 'independent':  a.SET(1, y)
    elif relation == 'double':     a.ADD(1, 0, 0)
    elif relation == 'plus_k':     a.SET(90, y-x); a.ADD(1, 0, 90)
    elif relation == 'square':     a.MUL(1, 0, 0)
    elif relation == 'complement': a.SET(90, 255); a.SUB(1, 90, 0)
    return a.build()

def measure_symmetry(x, y, relation):
    s_joint = prog_xy(x, y)
    s_x     = prog_x(x)
    s_cond  = prog_y_given_x(x, y, relation)
    ex = Exec(ext=EXT); ex.load(s_cond); ex.slots[0] = x; ex.run()
    return {
        'k_joint': len(s_joint)*8,
        'k_x':     len(s_x)*8,
        'k_cond':  len(s_cond)*8,
        'c_sym':   len(s_joint)*8 - len(s_x)*8 - len(s_cond)*8,
        'cond_ok': ex.slot(1) == y,
    }


# ── Self-interpreter (Direction B) ───────────────────────────────────────────
#
# Slot layout:
#   1-4    outer constants 0,1,2,3
#   5,6    outer constants 4,5
#   7      outer constant 8 (rule stride)
#   10     inner bit position
#   11     inner end bit position
#   12     token identity
#   17-19  decoded args
#   20-21  condition/comparison temps
#   30-31  address temps
#   40-42  value temps
#   50     inner program length
#   54-55  Elias subroutine state (k-countdown, accumulator)
#   60,62  jump offset temps
#   64-65  DEF loop temps
#   66     DEF at_type (read, discarded)
#   70     body_start (JIF/JMP base)
#   75     subroutine return site ID
#   80     subroutine result
#   85-90  rule-table and custom-op temps
#   200-299 inner slots
#   300-427 inner rule table
#   500+   inner program bits (1 bit per slot)
#
# c_interp measures interpreter logic bits only, independent of inner program
# size. Data storage costs ~23 bits per inner bit (SET instruction overhead),
# which is a separate O(|inner|) term, not part of the constant.

def _elias_sub(a):
    a.label('elias_sub')
    a.SET(54, 0)
    a.label('_sub_lz')
    a.SET(30, INNER_BASE); a.ADD(30, 30, 10)
    a.LOAD(31, 30);        a.ADD(10, 10, 2)
    a.EQ(20, 31, 1);       a.JIF(20, '_sub_val')
    a.ADD(54, 54, 2);      a.JMP('_sub_lz')
    a.label('_sub_val')
    a.SET(55, 1)
    a.label('_sub_lb')
    a.LT(20, 1, 54);       a.JIF(20, '_sub_done')
    a.MUL(55, 55, 3)
    a.SET(30, INNER_BASE); a.ADD(30, 30, 10)
    a.LOAD(31, 30);        a.ADD(10, 10, 2)
    a.ADD(55, 55, 31);     a.SUB(54, 54, 2)
    a.JMP('_sub_lb')
    a.label('_sub_done')
    a.SUB(80, 55, 2)


def _trampoline(a):
    """Binary-search dispatch back to call site stored in slot 75."""
    a.label('_trampoline')
    a.SET(90, 9);  a.LT(20, 75, 90); a.JIF(20, '_bs_r')
    a.SET(90, 4);  a.LT(20, 75, 90); a.JIF(20, '_bs_lr')
    a.SET(90, 2);  a.LT(20, 75, 90); a.JIF(20, '_bs_llr')
    a.SET(90, 1);  a.LT(20, 75, 90); a.JIF(20, '_bs_ret1')
    a.JMP('_ret_0')
    a.label('_bs_ret1');  a.JMP('_ret_1')
    a.label('_bs_llr')
    a.SET(90, 3);  a.LT(20, 75, 90); a.JIF(20, '_bs_ret3')
    a.JMP('_ret_2')
    a.label('_bs_ret3');  a.JMP('_ret_3')
    a.label('_bs_lr')
    a.SET(90, 6);  a.LT(20, 75, 90); a.JIF(20, '_bs_lrr')
    a.SET(90, 5);  a.LT(20, 75, 90); a.JIF(20, '_bs_ret5')
    a.JMP('_ret_4')
    a.label('_bs_ret5');  a.JMP('_ret_5')
    a.label('_bs_lrr')
    a.SET(90, 7);  a.LT(20, 75, 90); a.JIF(20, '_bs_lrr78')
    a.JMP('_ret_6')
    a.label('_bs_lrr78')
    a.SET(90, 8);  a.LT(20, 75, 90); a.JIF(20, '_bs_ret8')
    a.JMP('_ret_7')
    a.label('_bs_ret8');  a.JMP('_ret_8')
    a.label('_bs_r')
    a.SET(90, 13); a.LT(20, 75, 90); a.JIF(20, '_bs_rr')
    a.SET(90, 11); a.LT(20, 75, 90); a.JIF(20, '_bs_rlr')
    a.SET(90, 10); a.LT(20, 75, 90); a.JIF(20, '_bs_ret10')
    a.JMP('_ret_9')
    a.label('_bs_ret10'); a.JMP('_ret_10')
    a.label('_bs_rlr')
    a.SET(90, 12); a.LT(20, 75, 90); a.JIF(20, '_bs_ret12')
    a.JMP('_ret_11')
    a.label('_bs_ret12'); a.JMP('_ret_12')
    a.label('_bs_rr')
    a.SET(90, 15); a.LT(20, 75, 90); a.JIF(20, '_bs_rrr')
    a.SET(90, 14); a.LT(20, 75, 90); a.JIF(20, '_bs_ret14')
    a.JMP('_ret_13')
    a.label('_bs_ret14'); a.JMP('_ret_14')
    a.label('_bs_rrr')
    a.SET(90, 16); a.LT(20, 75, 90); a.JIF(20, '_bs_ret16')
    a.JMP('_ret_15')
    a.label('_bs_ret16'); a.JMP('_ret_16')
    a.JMP('interp_done')


def _call_elias(a, result_slot, site_id, return_label):
    a.SET(75, site_id)
    a.JMP('elias_sub')
    a.label(return_label)
    if result_slot != 80:
        a.ADD(result_slot, 80, 1)


def _read_token(a, dest, tag):
    tz = f'tz_{tag}'; to = f'to_{tag}'; te = f'te_{tag}'
    a.SET(dest, 0)
    a.label(tz)
    a.SET(30, INNER_BASE); a.ADD(30, 30, 10)
    a.LOAD(31, 30);        a.ADD(10, 10, 2)
    a.EQ(20, 31, 2);       a.JIF(20, f'tz_body_{tag}')
    a.JMP(to)
    a.label(f'tz_body_{tag}')
    a.ADD(dest, dest, 2)
    a.JMP(tz)
    a.label(to)
    a.SET(30, INNER_BASE); a.ADD(30, 30, 10)
    a.LOAD(31, 30);        a.ADD(10, 10, 2)
    a.EQ(20, 31, 2);       a.JIF(20, te)
    a.JMP(to)
    a.label(te)


def build_interpreter(inner_stream):
    """Build the self-interpreter loaded with inner_stream as its input.
    Returns (program_bytes, asm_object).
    Result of inner program ends up in slot ISLOT_BASE (200)."""
    inner_bits = stream_to_bits(inner_stream)
    a = Asm(); a.std()

    for i, b in enumerate(inner_bits):
        a.SET(INNER_BASE + i, b)

    a.SET(1, 0); a.SET(2, 1); a.SET(3, 2); a.SET(4, 3)
    a.SET(5, 4); a.SET(6, 5); a.SET(7, 8)

    a.SET(10, 0)
    _call_elias(a, 50, 0, '_ret_0')
    a.ADD(70, 10, 1)
    a.ADD(11, 10, 50)

    a.label('interp_loop')
    a.LT(20, 10, 11); a.JIF(20, 'interp_done')

    _read_token(a, 12, 'main')

    a.EQ(20, 12, 2); a.JIF(20, 'not_set'); a.JMP('do_set')
    a.label('not_set')
    a.EQ(20, 12, 3); a.JIF(20, 'not_add'); a.JMP('do_add')
    a.label('not_add')
    a.EQ(20, 12, 4); a.JIF(20, 'not_jif'); a.JMP('do_jif')
    a.label('not_jif')
    a.EQ(20, 12, 5); a.JIF(20, 'not_jmp'); a.JMP('do_jmp')
    a.label('not_jmp')
    a.EQ(20, 12, 6); a.JIF(20, 'not_def'); a.JMP('do_def')
    a.label('not_def')
    a.JMP('do_custom')

    a.label('do_set')
    _call_elias(a, 17, 1, '_ret_1')
    _call_elias(a, 18, 2, '_ret_2')
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 17); a.STOR(30, 18)
    a.JMP('interp_loop')

    a.label('do_add')
    _call_elias(a, 17, 3, '_ret_3')
    _call_elias(a, 18, 4, '_ret_4')
    _call_elias(a, 19, 5, '_ret_5')
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 18); a.LOAD(40, 30)
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 19); a.LOAD(41, 30)
    a.ADD(42, 40, 41)
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 17); a.STOR(30, 42)
    a.JMP('interp_loop')

    a.label('do_jif')
    _call_elias(a, 17, 6, '_ret_6')
    _call_elias(a, 60, 7, '_ret_7')
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 17); a.LOAD(62, 30)
    a.EQ(20, 62, 1); a.JIF(20, 'interp_loop')
    a.ADD(10, 70, 60)
    a.JMP('interp_loop')

    a.label('do_jmp')
    _call_elias(a, 60, 8, '_ret_8')
    a.ADD(10, 70, 60)
    a.JMP('interp_loop')

    a.label('do_def')
    _call_elias(a, 17, 9,  '_ret_9')
    _call_elias(a, 18, 10, '_ret_10')
    a.ADD(64, 18, 2)
    a.SET(85, RULE_BASE)
    a.SET(86, 6);  a.SUB(86, 17, 86)
    a.MUL(86, 86, 7); a.ADD(85, 85, 86)
    a.SET(30, 1); a.ADD(30, 30, 85); a.STOR(30, 64)
    a.SET(65, 0)
    a.label('_def_at_loop')
    a.LT(20, 65, 64); a.JIF(20, '_def_at_done')
    _call_elias(a, 66, 11, '_ret_11')
    a.ADD(65, 65, 2); a.JMP('_def_at_loop')
    a.label('_def_at_done')
    _call_elias(a, 17, 12, '_ret_12')
    _call_elias(a, 18, 13, '_ret_13')
    a.SET(30, 6); a.ADD(30, 30, 85); a.STOR(30, 17)
    a.SET(30, 7); a.ADD(30, 30, 85); a.STOR(30, 18)
    a.SET(30, 0); a.ADD(30, 30, 85); a.SET(86, 1); a.STOR(30, 86)
    a.JMP('interp_loop')

    a.label('do_custom')
    a.SET(85, RULE_BASE)
    a.SET(86, 6);  a.SUB(86, 12, 86)
    a.MUL(86, 86, 7); a.ADD(85, 85, 86)
    a.SET(30, 0); a.ADD(30, 30, 85); a.LOAD(88, 30)
    a.EQ(20, 88, 2); a.JIF(20, 'interp_loop')
    a.SET(30, 1); a.ADD(30, 30, 85); a.LOAD(64, 30)
    _call_elias(a, 17, 14, '_ret_14')
    a.SET(90, 2); a.LT(20, 64, 90); a.NOT(21, 20); a.JIF(21, '_cop_skip1')
    _call_elias(a, 18, 15, '_ret_15')
    a.label('_cop_skip1')
    a.SET(90, 3); a.LT(20, 64, 90); a.NOT(21, 20); a.JIF(21, '_cop_skip2')
    _call_elias(a, 19, 16, '_ret_16')
    a.label('_cop_skip2')
    a.SET(30, 6); a.ADD(30, 30, 85); a.LOAD(86, 30)
    a.SET(30, 7); a.ADD(30, 30, 85); a.LOAD(87, 30)
    a.EQ(20, 87, 2); a.JIF(20, '_cop_not_set')
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 17); a.STOR(30, 18)
    a.JMP('interp_loop')
    a.label('_cop_not_set')
    a.EQ(20, 87, 3); a.JIF(20, 'interp_loop')
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 18); a.LOAD(40, 30)
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 19); a.LOAD(41, 30)
    a.ADD(42, 40, 41)
    a.SET(30, ISLOT_BASE); a.ADD(30, 30, 17); a.STOR(30, 42)
    a.JMP('interp_loop')

    a.label('interp_done')
    a.SET(30, ISLOT_BASE); a.LOAD(0, 30)
    a.JMP('_end')

    _elias_sub(a)
    _trampoline(a)

    a.label('_end')
    return a.build(), a


def interpreter_logic_bits(asm_obj, n_inner_bits):
    """Return the number of bits attributable to interpreter logic vs. data storage.

    Data storage is the O(|inner|) cost of SET instructions that load inner bits
    into slots 500+. This grows linearly with inner program size and is not part
    of the constant. Logic bits are what remains — the interpreter machinery itself.

    Note: data_bits uses the actual per-bit cost of SET(500+i, v), which is ~23
    bits/bit at low slot indices. This is an encoding overhead of this storage
    scheme, not a fundamental lower bound on data cost."""
    bs = BS()
    data_bits = sum(
        bs.tlen(1,0) + bs.elen(INNER_BASE + i) + bs.elen(0)
        for i in range(n_inner_bits)
    )
    return asm_obj.prog_bits() - data_bits, data_bits
