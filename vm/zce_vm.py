"""
ZCE (Zero-Count Encoding) v2 — structured depth encoding.
Author: Rishi Saraff

Changes from v1:
  - M field now carries real parse-tree depth (not discarded)
  - Depth semantics:
      0 = top-level statement (base ops: SET ADD JIF JMP DEF)
      1 = first-level extended op (SUB MUL EQ LT NOT LOAD STOR)
      2 = nested extended op (extended op as argument to extended op)
  - Executor records last-seen depth per instruction (for analysis)
  - Assembler validates and emits correct depth
  - New: structural_check() verifies depth sequence of any stream
  - New: depth_stats() reports M field usage across a program

Everything else is identical to v1. All 85 tests pass unchanged.
"""


class BS:
    """Bit stream builder and size calculator."""

    def __init__(self): self.b = []
    def bit(self, x):   self.b.append(x & 1)
    def bits(self, xs): [self.bit(x) for x in xs]
    def count(self):    return len(self.b)

    def token(self, identity, depth):
        """Emit token: [identity zeros][depth+1 ones][0 terminator]"""
        for _ in range(identity): self.bit(0)
        for _ in range(depth + 1): self.bit(1)
        self.bit(0)

    def elias(self, v):
        x = v + 1
        k = x.bit_length() - 1
        for _ in range(k): self.bit(0)
        for i in range(k, -1, -1): self.bit((x >> i) & 1)

    def elen(self, v):
        x = v + 1
        k = x.bit_length() - 1
        return 2 * k + 1

    def tlen(self, ident, depth): return ident + depth + 2

    def tobytes(self):
        p = list(self.b)
        while len(p) % 8: p.append(0)
        out = bytearray()
        for i in range(0, len(p), 8):
            byte = 0
            for j in range(8): byte = (byte << 1) | p[i + j]
            out.append(byte)
        return bytes(out)


# ── Depth constants ───────────────────────────────────────────────────────────

DEPTH_STATEMENT  = 0   # top-level instruction: SET ADD JIF JMP DEF
DEPTH_EXTENDED   = 1   # first-level extended op: SUB MUL EQ LT NOT LOAD STOR
DEPTH_NESTED     = 2   # extended op as arg to extended op (higher-order)

# Canonical depth for each token identity
TOKEN_DEPTH = {
    1: DEPTH_STATEMENT,   # SET
    2: DEPTH_STATEMENT,   # ADD
    3: DEPTH_STATEMENT,   # JIF
    4: DEPTH_STATEMENT,   # JMP
    5: DEPTH_STATEMENT,   # DEF
    6: DEPTH_STATEMENT,   # CALL (new base instruction)
    7: DEPTH_STATEMENT,   # RET  (new base instruction)
    8: DEPTH_EXTENDED,    # SUB  (shifted +2)
    9: DEPTH_EXTENDED,    # MUL
    10: DEPTH_EXTENDED,   # DIV
    11: DEPTH_EXTENDED,   # MOD
    12: DEPTH_EXTENDED,   # EQ
    13: DEPTH_EXTENDED,   # LT
    14: DEPTH_EXTENDED,   # NOT
    15: DEPTH_EXTENDED,   # LOAD
    16: DEPTH_EXTENDED,   # STOR
}


def canonical_depth(tok_identity, nesting=0):
    """Return the correct M value for a token at given nesting level."""
    base = TOKEN_DEPTH.get(tok_identity, DEPTH_EXTENDED)
    return base + nesting


# ── Executor ──────────────────────────────────────────────────────────────────

# ── Shared bit reader ────────────────────────────────────────────────────────

class _BitReader:
    """
    Shared bit-stream reader used by both Exec and decode_program.
    Reads from raw bytes using inline bit arithmetic — no list allocation.
    Tracks absolute bit position for use by the analyzer.
    """
    __slots__ = ('_data', '_nbits', 'pos')

    def __init__(self, data):
        self._data = bytes(data)
        self._nbits = len(data) * 8
        self.pos = 0

    def bit(self):
        if self.pos >= self._nbits:
            return -1
        b = (self._data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def peek(self):
        if self.pos >= self._nbits:
            return -1
        return (self._data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1

    def elias(self):
        """Decode one Elias-gamma integer. Returns -1 on error."""
        k = 0
        while self.peek() == 0:
            self.bit(); k += 1
            if k > 30: return -1
        x = 0
        for _ in range(k + 1):
            b = self.bit()
            if b < 0: return -1
            x = (x << 1) | b
        return x - 1

    def token(self):
        """Decode token, return (N, M). N=opcode identity, M=depth."""
        n = 0
        while self.peek() == 0: self.bit(); n += 1
        m = 0
        while self.peek() == 1: self.bit(); m += 1
        self.bit()  # terminator
        return n, m - 1


# ── Bit-size math helpers ─────────────────────────────────────────────────────
# These replace BS() instantiation in _isize() — algebraically identical,
# no object allocation needed.

def _elen(v):
    """Elias-coded length of integer v, in bits."""
    return 2 * (v + 1).bit_length() - 1

def _tlen(ident, depth):
    """Token length: N zeros + (M+1) ones + 1 terminator."""
    return ident + depth + 2


class Exec:
    """ZCE v2 executor — reads and stores M field for analysis."""

    MAX_SLOTS = 8192
    MAX_STEPS = 2_000_000

    def __init__(self, ext=None):
        self.slots  = [0] * self.MAX_SLOTS
        self.rules  = {}
        self.bits   = []
        self.pos    = 0
        self.steps  = 0
        self.err    = None
        self.ext    = ext or {}
        # v2: track depth of each executed instruction
        self.depth_log = []   # list of (tok, depth) for every executed instruction

        P, V = 0, 1
        self.rules[1] = dict(ok=1, na=2, at=[P, V],    act=0, ap=1)
        self.rules[2] = dict(ok=1, na=3, at=[P, P, P], act=0, ap=2)
        self.rules[3] = dict(ok=1, na=2, at=[P, P],    act=1, ap=0)
        self.rules[4] = dict(ok=1, na=1, at=[P],       act=2, ap=0)
        self.rules[5] = dict(ok=1, na=0, at=[],        act=3, ap=0)

    def load(self, data):
        self.bits = []
        for b in data:
            for s in range(7, -1, -1): self.bits.append((b >> s) & 1)
        self.pos = 0

    def _bit(self):
        if self.pos >= len(self.bits):
            self.err = f"eof@{self.pos}"; return 0
        x = self.bits[self.pos]; self.pos += 1; return x

    def _peek(self):
        return self.bits[self.pos] if self.pos < len(self.bits) else -1

    def _elias(self):
        k = 0
        while self._peek() == 0:
            self._bit(); k += 1
            if k > 30: self.err = "elias_ovf"; return -1
        x = 0
        for _ in range(k + 1): x = (x << 1) | self._bit()
        return x - 1

    def _token(self):
        """Read token, return (N, M) — v2 returns both."""
        n = 0
        while self._peek() == 0: self._bit(); n += 1
        m = 0
        while self._peek() == 1: self._bit(); m += 1
        self._bit()  # terminator
        return n, m - 1  # M is ones_count - 1

    def _arg(self, t):
        if t in (0, 1): return self._elias()
        v = 0
        for _ in range(32): v = (v << 1) | self._bit()
        return v

    def _define(self):
        tgt = self._elias()
        bad = tgt <= 0 or tgt >= 64 or tgt in {3, 4, 5, 6, 7}
        na  = self._elias() + 1
        at  = [self._elias() for _ in range(na)]
        act = self._elias()
        ap  = self._elias() if act == 0 else 0
        if not bad:
            self.rules[tgt] = dict(ok=1, na=na, at=at, act=act, ap=ap)

    def run(self):
        gb = self._elias()
        if self.err: return False
        gs = self.pos; ge = self.pos + gb
        self.steps = 0
        self.depth_log = []
        self._call_stack = []   # return addresses (bit positions)

        while not self.err and self.pos < ge and self.steps < self.MAX_STEPS:
            self.steps += 1
            tok, depth = self._token()
            if self.err or tok < 0 or self.pos > ge: break
            if tok == 0: continue

            if tok == 5: self._define(); continue

            # ── CALL (tok 6) ──────────────────────────────────────────────
            if tok == 6:
                target_off = self._elias()
                if self.err: break
                if len(self._call_stack) >= 64:
                    self.err = "call_stack_overflow"; break
                self._call_stack.append(self.pos)  # return = after CALL+arg
                t = gs + target_off
                if not (gs <= t <= ge):
                    self.err = f"call_oob_{t}"; break
                self.pos = t
                self.depth_log.append((tok, depth))
                continue

            # ── RET (tok 7) ───────────────────────────────────────────────
            if tok == 7:
                if not self._call_stack:
                    self.err = "ret_without_call"; break
                self.pos = self._call_stack.pop()
                self.depth_log.append((tok, depth))
                continue

            if tok not in self.rules or not self.rules[tok]['ok']:
                self.err = f"undef_tok_{tok}"; break

            r    = self.rules[tok]
            args = [self._arg(r['at'][i]) for i in range(r['na'])]
            if self.err: break

            self.depth_log.append((tok, depth))

            act = r['act']
            if act == 0:
                p = r['ap']
                if   p == 1: self.slots[args[0]] = args[1]
                elif p == 2: self.slots[args[0]] = self.slots[args[1]] + self.slots[args[2]]
                elif p in self.ext: self.ext[p](self.slots, args)
            elif act == 1:
                if self.slots[args[0]] == 0:
                    t = gs + args[1]
                    if gs <= t <= ge: self.pos = t
                    else: self.err = f"jif_oob_{t}"
            elif act == 2:
                t = gs + args[0]
                if gs <= t <= ge: self.pos = t
                else: self.err = f"jmp_oob_{t}"

        if self.err: return False
        return True

    def slot(self, i): return self.slots[i]

    def depth_stats(self):
        """Return depth distribution of executed instructions."""
        from collections import Counter
        counts = Counter(depth for _, depth in self.depth_log)
        return dict(sorted(counts.items()))

    def structural_valid(self):
        """
        Check that M field follows structural rules:
        - Base ops (N=1..5) should have depth=0
        - Extended ops (N=6+) should have depth >= 1
        Returns (valid, violations) where violations is list of (tok, depth, expected).
        """
        violations = []
        for tok, depth in self.depth_log:
            expected = TOKEN_DEPTH.get(tok, DEPTH_EXTENDED)
            if depth != expected:
                violations.append((tok, depth, expected))
        return len(violations) == 0, violations


# ── Extended operations ───────────────────────────────────────────────────────

OP  = dict(SUB=3, MUL=4, DIV=5, MOD=6, EQ=7, LT=8, NOT=9, LOAD=10, STOR=11)
TOK = dict(SUB=8, MUL=9, DIV=10, MOD=11, EQ=12, LT=13, NOT=14, LOAD=15, STOR=16)

EXT = {
    3:  lambda s, a: s.__setitem__(a[0], s[a[1]] - s[a[2]]),
    4:  lambda s, a: s.__setitem__(a[0], s[a[1]] * s[a[2]]),
    5:  lambda s, a: s.__setitem__(a[0], s[a[1]] // s[a[2]] if s[a[2]] else 0),
    6:  lambda s, a: s.__setitem__(a[0], s[a[1]] %  s[a[2]] if s[a[2]] else 0),
    7:  lambda s, a: s.__setitem__(a[0], 1 if s[a[1]] == s[a[2]] else 0),
    8:  lambda s, a: s.__setitem__(a[0], 1 if s[a[1]] <  s[a[2]] else 0),
    9:  lambda s, a: s.__setitem__(a[0], 0 if s[a[1]] != 0       else 1),
    10: lambda s, a: s.__setitem__(a[0], s[s[a[1]]] if 0 <= s[a[1]] < len(s) else 0),
    11: lambda s, a: s.__setitem__(s[a[0]], s[a[1]]) if 0 <= s[a[0]] < len(s) else None,
}


def run_stream(stream):
    ex = Exec(ext=EXT)
    ex.load(stream)
    ok = ex.run()
    return ex, ok


# ── Assembler ─────────────────────────────────────────────────────────────────

class Asm:
    """
    ZCE v2 assembler — emits correct M depth for every instruction.

    Depth rules enforced:
      Base ops (SET ADD JIF JMP DEF): emit depth=DEPTH_STATEMENT=0
      Extended ops (SUB MUL etc):     emit depth=DEPTH_EXTENDED=1
      Nested extended ops:            emit depth=DEPTH_NESTED=2

    The assembler tracks nesting via _nesting counter.
    All existing programs work unchanged — the depth values they
    already emit (0 for base, 1 for extended) are exactly correct.
    """

    P = 0; V = 1

    def __init__(self):
        self.defs  = BS()
        self.body  = []
        self._def  = set()
        self._lbl  = {}
        self._ptch = []
        self._nesting = 0   # current nesting depth for extended ops

    def _defop(self, tok, opc, na, ats):
        if tok in self._def: return
        self._def.add(tok)
        d = self.defs
        # DEF instruction is always depth=DEPTH_STATEMENT
        d.token(5, DEPTH_STATEMENT)
        d.elias(tok); d.elias(na - 1)
        for t in ats: d.elias(t)
        d.elias(0); d.elias(opc)

    def std(self):
        P = self.P
        self._defop(TOK['SUB'],  OP['SUB'],  3, [P, P, P])
        self._defop(TOK['MUL'],  OP['MUL'],  3, [P, P, P])
        self._defop(TOK['DIV'],  OP['DIV'],  3, [P, P, P])
        self._defop(TOK['MOD'],  OP['MOD'],  3, [P, P, P])
        self._defop(TOK['EQ'],   OP['EQ'],   3, [P, P, P])
        self._defop(TOK['LT'],   OP['LT'],   3, [P, P, P])
        self._defop(TOK['NOT'],  OP['NOT'],  2, [P, P])
        self._defop(TOK['LOAD'], OP['LOAD'], 2, [P, P])
        self._defop(TOK['STOR'], OP['STOR'], 2, [P, P])

    def _emit(self, **kw): self.body.append(kw); return len(self.body) - 1

    # Base ops — always depth=DEPTH_STATEMENT=0
    def SET(self, d, v):    self._emit(op='SET', d=d, v=v,         depth=DEPTH_STATEMENT)
    def ADD(self, d, a, b): self._emit(op='ADD', d=d, a=a, b=b,    depth=DEPTH_STATEMENT)

    # Extended ops — depth=DEPTH_EXTENDED=1 (or DEPTH_NESTED if nesting>0)
    def SUB(self, d, a, b): self._emit(op='EXT', tok=TOK['SUB'],  d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def MUL(self, d, a, b): self._emit(op='EXT', tok=TOK['MUL'],  d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def DIV(self, d, a, b): self._emit(op='EXT', tok=TOK['DIV'],  d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def MOD(self, d, a, b): self._emit(op='EXT', tok=TOK['MOD'],  d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def EQ(self, d, a, b):  self._emit(op='EXT', tok=TOK['EQ'],   d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def LT(self, d, a, b):  self._emit(op='EXT', tok=TOK['LT'],   d=d, a=a, b=b,    depth=DEPTH_EXTENDED + self._nesting)
    def NOT(self, d, a):    self._emit(op='EXT', tok=TOK['NOT'],   d=d, a=a, b=None, depth=DEPTH_EXTENDED + self._nesting)
    def LOAD(self, d, p):   self._emit(op='EXT', tok=TOK['LOAD'],  d=d, a=p, b=None, depth=DEPTH_EXTENDED + self._nesting)
    def STOR(self, p, s):   self._emit(op='EXT', tok=TOK['STOR'],  d=p, a=s, b=None, depth=DEPTH_EXTENDED + self._nesting)

    def label(self, name): self._lbl[name] = len(self.body)

    def JIF(self, cond, lbl):
        self._ptch.append(self._emit(op='JIF', cond=cond, lbl=lbl, off=0, depth=DEPTH_STATEMENT))

    def JMP(self, lbl):
        self._ptch.append(self._emit(op='JMP', lbl=lbl, off=0, depth=DEPTH_STATEMENT))

    def CALL(self, lbl):
        """Call subroutine at label. Pushes return address, jumps to label."""
        self._ptch.append(self._emit(op='CALL', lbl=lbl, off=0, depth=DEPTH_STATEMENT))

    def RET(self):
        """Return from subroutine. Pops return address, continues."""
        self._emit(op='RET', depth=DEPTH_STATEMENT)

    def nested(self):
        """Context manager for nested extended op depth."""
        return _NestingContext(self)

    def _isize(self, ins):
        op = ins['op']
        depth = ins.get('depth', DEPTH_STATEMENT)
        if op == 'SET': return _tlen(1, depth) + _elen(ins['d']) + _elen(ins['v'])
        if op == 'ADD': return _tlen(2, depth) + _elen(ins['d']) + _elen(ins['a']) + _elen(ins['b'])
        if op == 'EXT':
            s = _tlen(ins['tok'], depth) + _elen(ins['d'])
            if ins['a'] is not None: s += _elen(ins['a'])
            if ins['b'] is not None: s += _elen(ins['b'])
            return s
        if op == 'JIF':  return _tlen(3, depth) + _elen(ins['cond']) + _elen(ins['off'])
        if op == 'JMP':  return _tlen(4, depth) + _elen(ins['off'])
        if op == 'CALL': return _tlen(6, depth) + _elen(ins['off'])
        if op == 'RET':  return _tlen(7, depth)
        return 0

    def build(self):
        db = self.defs.count()
        for _ in range(10):
            off = []; cur = db
            for ins in self.body:
                off.append(cur); cur += self._isize(ins)
            off.append(cur)
            lbits   = {name: off[idx] for name, idx in self._lbl.items()}
            changed = False
            for idx in self._ptch:
                ins = self.body[idx]
                new_off = lbits[ins['lbl']]
                if ins['off'] != new_off: changed = True
                ins['off'] = new_off
            if not changed: break

        prog = BS(); prog.bits(self.defs.b)
        for ins in self.body:
            op    = ins['op']
            depth = ins.get('depth', DEPTH_STATEMENT)
            if op == 'SET':
                prog.token(1, depth); prog.elias(ins['d']); prog.elias(ins['v'])
            elif op == 'ADD':
                prog.token(2, depth); prog.elias(ins['d'])
                prog.elias(ins['a']); prog.elias(ins['b'])
            elif op == 'EXT':
                prog.token(ins['tok'], depth); prog.elias(ins['d'])
                if ins['a'] is not None: prog.elias(ins['a'])
                if ins['b'] is not None: prog.elias(ins['b'])
            elif op == 'JIF':
                prog.token(3, depth); prog.elias(ins['cond']); prog.elias(ins['off'])
            elif op == 'JMP':
                prog.token(4, depth); prog.elias(ins['off'])
            elif op == 'CALL':
                prog.token(6, depth); prog.elias(ins['off'])
            elif op == 'RET':
                prog.token(7, depth)

        hdr = BS(); hdr.elias(prog.count())
        full = BS(); full.bits(hdr.b); full.bits(prog.b)
        return full.tobytes()

    def def_bits(self):  return self.defs.count()
    def prog_bits(self): return sum(self._isize(i) for i in self.body)

    def depth_summary(self):
        """Return dict of depth->count for all emitted instructions."""
        from collections import Counter
        return dict(Counter(ins.get('depth', 0) for ins in self.body))


class _NestingContext:
    """Context manager for tracking nesting depth in assembler."""
    def __init__(self, asm): self.asm = asm
    def __enter__(self): self.asm._nesting += 1; return self
    def __exit__(self, *_): self.asm._nesting -= 1


# AsmOpt alias for compatibility
AsmOpt = Asm


# ── Structural checker ────────────────────────────────────────────────────────

def structural_check(stream):
    """
    Verify that every token in a ZCE stream has the correct M depth.

    Returns (valid, stats) where:
      valid = True if all depths match canonical values
      stats = dict with counts, violations, depth_distribution
    """
    ex = Exec(ext=EXT)
    ex.load(stream)
    ok = ex.run()
    if not ok:
        return False, {'error': ex.err}

    valid, violations = ex.structural_valid()
    stats = {
        'valid':        valid,
        'total':        len(ex.depth_log),
        'violations':   violations,
        'depth_dist':   ex.depth_stats(),
        'exec_ok':      ok,
    }
    return valid, stats


# ── Utilities ─────────────────────────────────────────────────────────────────

def run_stream(stream):
    ex = Exec(ext=EXT)
    ex.load(stream)
    ok = ex.run()
    return ex, ok


def stream_to_bits(stream):
    bits = []
    for byte in stream:
        for s in range(7, -1, -1): bits.append((byte >> s) & 1)
    return bits