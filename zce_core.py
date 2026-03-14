"""
ZCE (Zero-Count Encoding) — core language implementation.
Author: Rishi Saraff
"""


class BS:
    """Bit stream builder and size calculator."""

    def __init__(self): self.b = []
    def bit(self, x):   self.b.append(x & 1)
    def bits(self, xs): [self.bit(x) for x in xs]
    def count(self):    return len(self.b)

    def token(self, identity, depth):
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


class Exec:
    """ZCE executor."""

    MAX_SLOTS = 4096
    MAX_STEPS = 2_000_000

    def __init__(self, ext=None):
        self.slots = [0] * self.MAX_SLOTS
        self.rules = {}
        self.bits  = []
        self.pos   = 0
        self.steps = 0
        self.err   = None
        self.ext   = ext or {}
        P, V = 0, 1
        self.rules[1] = dict(ok=1, na=2, at=[P, V],    act=0, ap=1)
        self.rules[2] = dict(ok=1, na=3, at=[P, P, P], act=0, ap=2)
        self.rules[3] = dict(ok=1, na=2, at=[P, 2],    act=1, ap=0)
        self.rules[4] = dict(ok=1, na=1, at=[2],       act=2, ap=0)
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
        n = 0
        while self._peek() == 0: self._bit(); n += 1
        while self._peek() == 1: self._bit()
        self._bit()
        return n

    def _arg(self, t):
        if t in (0, 1): return self._elias()
        v = 0
        for _ in range(32): v = (v << 1) | self._bit()
        return v

    def _define(self):
        tgt = self._elias()
        bad = tgt <= 0 or tgt >= 64 or tgt in {3, 4, 5}
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
        while not self.err and self.pos < ge and self.steps < self.MAX_STEPS:
            self.steps += 1
            tok = self._token()
            if self.err or tok < 0 or self.pos > ge: break
            if tok == 0: continue
            if tok == 5: self._define(); continue
            if tok not in self.rules or not self.rules[tok]['ok']:
                self.err = f"undef_tok_{tok}"; break
            r    = self.rules[tok]
            args = [self._arg(r['at'][i]) for i in range(r['na'])]
            if self.err: break
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


# Extended operations: op-code → slot-mutating lambda
OP  = dict(SUB=3, MUL=4, DIV=5, MOD=6, EQ=7, LT=8, NOT=9, LOAD=10, STOR=11)
TOK = dict(SUB=6, MUL=7, DIV=8, MOD=9, EQ=10, LT=11, NOT=12, LOAD=13, STOR=14)

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


class Asm:
    """ZCE assembler with 32-bit jump offsets."""

    P = 0; V = 1

    def __init__(self):
        self.defs  = BS()
        self.body  = []
        self._def  = set()
        self._lbl  = {}
        self._ptch = []

    def _defop(self, tok, opc, na, ats):
        if tok in self._def: return
        self._def.add(tok)
        d = self.defs
        d.token(5, 1); d.elias(tok); d.elias(na - 1)
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

    def SET(self, d, v):    self._emit(op='SET', d=d, v=v)
    def ADD(self, d, a, b): self._emit(op='ADD', d=d, a=a, b=b)
    def SUB(self, d, a, b): self._emit(op='EXT', tok=TOK['SUB'], d=d, a=a, b=b)
    def MUL(self, d, a, b): self._emit(op='EXT', tok=TOK['MUL'], d=d, a=a, b=b)
    def DIV(self, d, a, b): self._emit(op='EXT', tok=TOK['DIV'], d=d, a=a, b=b)
    def MOD(self, d, a, b): self._emit(op='EXT', tok=TOK['MOD'], d=d, a=a, b=b)
    def EQ(self, d, a, b):  self._emit(op='EXT', tok=TOK['EQ'],  d=d, a=a, b=b)
    def LT(self, d, a, b):  self._emit(op='EXT', tok=TOK['LT'],  d=d, a=a, b=b)
    def NOT(self, d, a):    self._emit(op='EXT', tok=TOK['NOT'],  d=d, a=a, b=None)
    def LOAD(self, d, p):   self._emit(op='EXT', tok=TOK['LOAD'], d=d, a=p, b=None)
    def STOR(self, p, s):   self._emit(op='EXT', tok=TOK['STOR'], d=p, a=s, b=None)

    def label(self, name): self._lbl[name] = len(self.body)

    def JIF(self, cond, lbl):
        self._ptch.append(self._emit(op='JIF', cond=cond, lbl=lbl, off=0))

    def JMP(self, lbl):
        self._ptch.append(self._emit(op='JMP', lbl=lbl, off=0))

    def _isize(self, ins):
        bs = BS()
        op = ins['op']
        if op == 'SET': return bs.tlen(1,0) + bs.elen(ins['d']) + bs.elen(ins['v'])
        if op == 'ADD': return bs.tlen(2,0) + bs.elen(ins['d']) + bs.elen(ins['a']) + bs.elen(ins['b'])
        if op == 'EXT':
            s = bs.tlen(ins['tok'], 1) + bs.elen(ins['d'])
            if ins['a'] is not None: s += bs.elen(ins['a'])
            if ins['b'] is not None: s += bs.elen(ins['b'])
            return s
        if op == 'JIF': return bs.tlen(3,0) + bs.elen(ins['cond']) + 32
        if op == 'JMP': return bs.tlen(4,0) + 32
        return 0

    def build(self):
        db = self.defs.count()
        for _ in range(5):
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
            op = ins['op']
            if op == 'SET':
                prog.token(1,0); prog.elias(ins['d']); prog.elias(ins['v'])
            elif op == 'ADD':
                prog.token(2,0); prog.elias(ins['d'])
                prog.elias(ins['a']); prog.elias(ins['b'])
            elif op == 'EXT':
                prog.token(ins['tok'],1); prog.elias(ins['d'])
                if ins['a'] is not None: prog.elias(ins['a'])
                if ins['b'] is not None: prog.elias(ins['b'])
            elif op == 'JIF':
                prog.token(3,0); prog.elias(ins['cond'])
                for bit in range(31,-1,-1): prog.bit((ins['off'] >> bit) & 1)
            elif op == 'JMP':
                prog.token(4,0)
                for bit in range(31,-1,-1): prog.bit((ins['off'] >> bit) & 1)

        hdr = BS(); hdr.elias(prog.count())
        full = BS(); full.bits(hdr.b); full.bits(prog.b)
        return full.tobytes()

    def def_bits(self):  return self.defs.count()
    def prog_bits(self): return sum(self._isize(i) for i in self.body)


class AsmOpt(Asm):
    """Assembler variant with Elias-coded jump offsets instead of 32-bit raw.
    Used for inner programs where jump offsets are small."""

    def _isize(self, ins):
        bs = BS()
        op = ins['op']
        if op == 'SET': return bs.tlen(1,0) + bs.elen(ins['d']) + bs.elen(ins['v'])
        if op == 'ADD': return bs.tlen(2,0) + bs.elen(ins['d']) + bs.elen(ins['a']) + bs.elen(ins['b'])
        if op == 'EXT':
            s = bs.tlen(ins['tok'], 1) + bs.elen(ins['d'])
            if ins['a'] is not None: s += bs.elen(ins['a'])
            if ins['b'] is not None: s += bs.elen(ins['b'])
            return s
        if op == 'JIF': return bs.tlen(3,0) + bs.elen(ins['cond']) + bs.elen(ins['off'])
        if op == 'JMP': return bs.tlen(4,0) + bs.elen(ins['off'])
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
            op = ins['op']
            if op == 'SET':
                prog.token(1,0); prog.elias(ins['d']); prog.elias(ins['v'])
            elif op == 'ADD':
                prog.token(2,0); prog.elias(ins['d'])
                prog.elias(ins['a']); prog.elias(ins['b'])
            elif op == 'EXT':
                prog.token(ins['tok'],1); prog.elias(ins['d'])
                if ins['a'] is not None: prog.elias(ins['a'])
                if ins['b'] is not None: prog.elias(ins['b'])
            elif op == 'JIF':
                prog.token(3,0); prog.elias(ins['cond'])
                prog.elias(ins['off'])
            elif op == 'JMP':
                prog.token(4,0)
                prog.elias(ins['off'])

        hdr = BS(); hdr.elias(prog.count())
        full = BS(); full.bits(hdr.b); full.bits(prog.b)
        return full.tobytes()


def stream_to_bits(stream):
    bits = []
    for byte in stream:
        for s in range(7, -1, -1): bits.append((byte >> s) & 1)
    return bits


def consts(a):
    """Emit slot constants 0-3 used by most programs."""
    a.SET(1, 0); a.SET(2, 1); a.SET(3, 2); a.SET(4, 3)
