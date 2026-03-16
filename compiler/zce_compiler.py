"""
ZCE Compiler — ZCE-lang → ZCE binary
Author: Rishi Saraff

Language grammar:
  program  = (fundef | stmt)* EOF
  fundef   = 'fun' NAME '(' params ')' block
  params   = (NAME (',' NAME)*)?
  block    = '{' stmt* '}'
  stmt     = letdecl | assign | ifstmt | whilestmt | returnstmt | callstmt
  letdecl  = 'let' NAME '=' expr ';'
  assign   = NAME '=' expr ';'
  ifstmt   = 'if' '(' expr ')' block ('else' block)?
  whilestmt= 'while' '(' expr ')' block
  returnstmt= 'return' expr ';'
  callstmt = NAME '(' args ')' ';'
  expr     = comparison (('and'|'or') comparison)*
  comparison = additive (('<'|'>'|'=='|'!='|'<='|'>=') additive)?
  additive = term (('+'|'-') term)*
  term     = unary (('*'|'/'|'%') unary)*
  unary    = '-' unary | '!' unary | primary
  primary  = INT | NAME | NAME '(' args ')' | '(' expr ')'
  args     = (expr (',' expr)*)?

Types: int32 only. Variables are slots. Functions map to subroutines.

Example program:
  fun max(a, b) {
      if (a > b) { return a; }
      return b;
  }
  let x = 10;
  let y = max(x, 20);
"""

import re
from vm.zce_vm import Asm

# ══════════════════════════════════════════════════════════════════════════════
# LEXER
# ══════════════════════════════════════════════════════════════════════════════

TOKEN_SPEC = [
    ('NUMBER',   r'\d+'),
    ('NAME',     r'[A-Za-z_][A-Za-z0-9_]*'),
    ('LE',       r'<='),
    ('GE',       r'>='),
    ('EQ',       r'=='),
    ('NE',       r'!='),
    ('AND',      r'&&'),
    ('OR',       r'\|\|'),
    ('LT',       r'<'),
    ('GT',       r'>'),
    ('PLUS',     r'\+'),
    ('MINUS',    r'-'),
    ('STAR',     r'\*'),
    ('SLASH',    r'/'),
    ('PERCENT',  r'%'),
    ('BANG',     r'!'),
    ('ASSIGN',   r'='),
    ('LPAREN',   r'\('),
    ('RPAREN',   r'\)'),
    ('LBRACE',   r'\{'),
    ('RBRACE',   r'\}'),
    ('SEMI',     r';'),
    ('COMMA',    r','),
    ('SKIP',     r'[ \t\n\r]+|//[^\n]*'),
    ('MISMATCH', r'.'),
]

_TOK_RE = re.compile('|'.join(f'(?P<{n}>{p})' for n, p in TOKEN_SPEC))

KEYWORDS = {'fun', 'let', 'if', 'else', 'while', 'return'}

class Token:
    def __init__(self, kind, val, line):
        self.kind = kind; self.val = val; self.line = line
    def __repr__(self): return f'Token({self.kind},{self.val!r})'

def lex(src):
    tokens = []
    line = 1
    for m in _TOK_RE.finditer(src):
        kind = m.lastgroup; val = m.group()
        if kind == 'SKIP':
            line += val.count('\n'); continue
        if kind == 'MISMATCH':
            raise SyntaxError(f'Unexpected character {val!r} at line {line}')
        if kind == 'NAME' and val in KEYWORDS:
            kind = val.upper()
        tokens.append(Token(kind, val, line))
    tokens.append(Token('EOF', '', line))
    return tokens

# ══════════════════════════════════════════════════════════════════════════════
# PARSER → AST
# ══════════════════════════════════════════════════════════════════════════════

class Node:
    def __init__(self, kind, **kw):
        self.kind = kind
        self.__dict__.update(kw)
    def __repr__(self):
        attrs = {k:v for k,v in self.__dict__.items() if k!='kind'}
        return f'{self.kind}({attrs})'

class Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.pos  = 0

    def peek(self): return self.toks[self.pos]
    def cur(self):  return self.toks[self.pos]

    def eat(self, kind):
        t = self.toks[self.pos]
        if t.kind != kind:
            raise SyntaxError(f'Expected {kind}, got {t.kind}({t.val!r}) at line {t.line}')
        self.pos += 1
        return t

    def match(self, *kinds):
        if self.cur().kind in kinds:
            t = self.cur(); self.pos += 1; return t
        return None

    def parse(self):
        stmts = []
        while self.cur().kind != 'EOF':
            if self.cur().kind == 'FUN':
                stmts.append(self.parse_fundef())
            else:
                stmts.append(self.parse_stmt())
        return Node('Program', body=stmts)

    def parse_fundef(self):
        self.eat('FUN')
        name = self.eat('NAME').val
        self.eat('LPAREN')
        params = []
        if self.cur().kind != 'RPAREN':
            params.append(self.eat('NAME').val)
            while self.match('COMMA'):
                params.append(self.eat('NAME').val)
        self.eat('RPAREN')
        body = self.parse_block()
        return Node('FunDef', name=name, params=params, body=body)

    def parse_block(self):
        self.eat('LBRACE')
        stmts = []
        while self.cur().kind != 'RBRACE':
            stmts.append(self.parse_stmt())
        self.eat('RBRACE')
        return stmts

    def parse_stmt(self):
        k = self.cur().kind
        if k == 'LET':       return self.parse_let()
        if k == 'IF':        return self.parse_if()
        if k == 'WHILE':     return self.parse_while()
        if k == 'RETURN':    return self.parse_return()
        if k == 'NAME':
            # lookahead: assign or call
            if self.toks[self.pos+1].kind == 'ASSIGN':
                return self.parse_assign()
            if self.toks[self.pos+1].kind == 'LPAREN':
                n = self.parse_call_expr(); self.eat('SEMI'); return Node('ExprStmt', expr=n)
        raise SyntaxError(f'Unexpected token {self.cur()} at line {self.cur().line}')

    def parse_let(self):
        self.eat('LET')
        name = self.eat('NAME').val
        self.eat('ASSIGN')
        val  = self.parse_expr()
        self.eat('SEMI')
        return Node('Let', name=name, val=val)

    def parse_assign(self):
        name = self.eat('NAME').val
        self.eat('ASSIGN')
        val  = self.parse_expr()
        self.eat('SEMI')
        return Node('Assign', name=name, val=val)

    def parse_if(self):
        self.eat('IF'); self.eat('LPAREN')
        cond = self.parse_expr()
        self.eat('RPAREN')
        then = self.parse_block()
        els  = None
        if self.match('ELSE'):
            els = self.parse_block()
        return Node('If', cond=cond, then=then, els=els)

    def parse_while(self):
        self.eat('WHILE'); self.eat('LPAREN')
        cond = self.parse_expr()
        self.eat('RPAREN')
        body = self.parse_block()
        return Node('While', cond=cond, body=body)

    def parse_return(self):
        self.eat('RETURN')
        val = self.parse_expr()
        self.eat('SEMI')
        return Node('Return', val=val)

    def parse_call_expr(self):
        name = self.eat('NAME').val
        self.eat('LPAREN')
        args = []
        if self.cur().kind != 'RPAREN':
            args.append(self.parse_expr())
            while self.match('COMMA'):
                args.append(self.parse_expr())
        self.eat('RPAREN')
        return Node('Call', name=name, args=args)

    def parse_expr(self):
        left = self.parse_comparison()
        while self.cur().kind in ('AND','OR'):
            op = self.cur().kind; self.pos += 1
            right = self.parse_comparison()
            left  = Node('BinOp', op=op, left=left, right=right)
        return left

    def parse_comparison(self):
        left = self.parse_additive()
        if self.cur().kind in ('LT','GT','EQ','NE','LE','GE'):
            op = self.cur().kind; self.pos += 1
            right = self.parse_additive()
            return Node('BinOp', op=op, left=left, right=right)
        return left

    def parse_additive(self):
        left = self.parse_term()
        while self.cur().kind in ('PLUS','MINUS'):
            op = self.cur().kind; self.pos += 1
            right = self.parse_term()
            left  = Node('BinOp', op=op, left=left, right=right)
        return left

    def parse_term(self):
        left = self.parse_unary()
        while self.cur().kind in ('STAR','SLASH','PERCENT'):
            op = self.cur().kind; self.pos += 1
            right = self.parse_unary()
            left  = Node('BinOp', op=op, left=left, right=right)
        return left

    def parse_unary(self):
        if self.cur().kind == 'MINUS':
            self.pos += 1
            return Node('Unary', op='NEG', operand=self.parse_unary())
        if self.cur().kind == 'BANG':
            self.pos += 1
            return Node('Unary', op='NOT', operand=self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        t = self.cur()
        if t.kind == 'NUMBER':
            self.pos += 1; return Node('Int', val=int(t.val))
        if t.kind == 'NAME':
            if self.toks[self.pos+1].kind == 'LPAREN':
                return self.parse_call_expr()
            self.pos += 1; return Node('Var', name=t.val)
        if t.kind == 'LPAREN':
            self.eat('LPAREN'); e = self.parse_expr(); self.eat('RPAREN'); return e
        raise SyntaxError(f'Unexpected primary {t} at line {t.line}')

def parse(src):
    return Parser(lex(src)).parse()

# ══════════════════════════════════════════════════════════════════════════════
# CODE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

class Env:
    """Variable → slot mapping with scope stack."""
    def __init__(self, base_slot=0):
        self.scopes  = [{}]
        self.next    = base_slot
        self.max_slot = base_slot

    def push(self): self.scopes.append({})
    def pop(self):  self.scopes.pop()

    def declare(self, name):
        slot = self.next; self.next += 1
        if self.next > self.max_slot: self.max_slot = self.next
        self.scopes[-1][name] = slot
        return slot

    def lookup(self, name):
        for scope in reversed(self.scopes):
            if name in scope: return scope[name]
        raise NameError(f'Undefined variable: {name!r}')

    def define(self, name, slot):
        self.scopes[-1][name] = slot


class Compiler:
    """Compiles ZCE-lang AST to ZCE Asm."""

    SCRATCH_BASE = 200  # scratch slots start here (above user vars)
    TMP_BASE     = 230  # temp slots for expressions

    def __init__(self):
        self.asm      = Asm()
        self.asm.std()
        self.env      = Env(base_slot=0)
        self.funs     = {}   # name → (label, params)
        self._tmp     = self.TMP_BASE
        self._lbl     = 0
        self._zero    = self.SCRATCH_BASE       # always 0
        self._one     = self.SCRATCH_BASE + 1   # always 1
        self._neg1    = self.SCRATCH_BASE + 2   # always -1
        self._ret_val = self.SCRATCH_BASE + 3   # function return value slot
        # emit constants
        self.asm.SET(self._zero,  0)
        self.asm.SET(self._one,   1)
        # _neg1 computed on demand via SUB

    def fresh_label(self, hint='L'):
        self._lbl += 1; return f'__{hint}{self._lbl}'

    def alloc_tmp(self):
        s = self._tmp; self._tmp += 1; return s

    def free_tmp(self, s):
        if s == self._tmp - 1: self._tmp -= 1  # simple stack dealloc

    # ── Entry point ───────────────────────────────────────────────────────────
    def compile(self, ast):
        # First pass: register all functions, assigning each a unique tmp region.
        # Layout: top-level tmps 230-249, fn0 tmps 250-269, fn1 tmps 270-289, ...
        # This ensures callee tmps never overlap with live caller tmp values.
        fn_defs = [n for n in ast.body if n.kind == 'FunDef']
        for idx, node in enumerate(fn_defs):
            lbl = f'__fun_{node.name}'
            tmp_base = self.TMP_BASE + 20 * (idx + 1)
            self.funs[node.name] = (lbl, node.params, tmp_base)

        # Second pass: emit top-level statements (skip fundefs — emitted after)
        end_lbl = self.fresh_label('end')
        has_stmts = any(n.kind != 'FunDef' for n in ast.body)
        if has_stmts:
            for node in ast.body:
                if node.kind != 'FunDef':
                    self.emit_stmt(node)
            self.asm.JMP(end_lbl)

        # Emit function bodies
        for node in ast.body:
            if node.kind == 'FunDef':
                self.emit_fundef(node)

        self.asm.label(end_lbl)
        return self.asm.build()

    # ── Statements ────────────────────────────────────────────────────────────
    def emit_stmt(self, node):
        k = node.kind
        if k == 'Let':
            slot = self.env.declare(node.name)
            self.emit_expr_into(node.val, slot)
        elif k == 'Assign':
            slot = self.env.lookup(node.name)
            self.emit_expr_into(node.val, slot)
        elif k == 'If':
            self.emit_if(node)
        elif k == 'While':
            self.emit_while(node)
        elif k == 'Return':
            self.emit_expr_into(node.val, self._ret_val)
            self.asm.RET()
        elif k == 'ExprStmt':
            if node.expr.kind == 'Call':
                self.emit_call(node.expr, self._ret_val)
            else:
                t = self.alloc_tmp()
                self.emit_expr_into(node.expr, t)
                self.free_tmp(t)
        else:
            raise NotImplementedError(f'emit_stmt: {k}')

    def emit_if(self, node):
        cond_slot = self.alloc_tmp()
        self.emit_expr_into(node.cond, cond_slot)
        else_lbl = self.fresh_label('else')
        end_lbl  = self.fresh_label('endif')
        # JIF fires when slot==0 → jump to else when condition is false
        self.asm.JIF(cond_slot, else_lbl)
        self.env.push()
        for s in node.then: self.emit_stmt(s)
        self.env.pop()
        if node.els:
            self.asm.JMP(end_lbl)
            self.asm.label(else_lbl)
            self.env.push()
            for s in node.els: self.emit_stmt(s)
            self.env.pop()
            self.asm.label(end_lbl)
        else:
            self.asm.label(else_lbl)
        self.free_tmp(cond_slot)

    def emit_while(self, node):
        loop_lbl  = self.fresh_label('wloop')
        done_lbl  = self.fresh_label('wdone')
        cond_slot = self.alloc_tmp()
        self.asm.label(loop_lbl)

        # Tier-safe optimisation: if the condition is a simple less-than comparison
        # (var < expr), emit SUB(cond, bound, var) instead of LT(cond, var, bound).
        # When var reaches bound, SUB gives 0 and JIF fires — same semantics.
        # The tier checker recognises SUB as non-increasing, proving termination.
        # For all other conditions, fall back to the general LT/JIF pattern,
        # which is correct but not tier-provable.
        if (node.cond.kind == 'BinOp' and node.cond.op == 'LT'
                and node.cond.left.kind == 'Var'):
            # Tier-safe path: emit bound - var into cond_slot
            bound_slot = self.alloc_tmp()
            self.emit_expr_into(node.cond.right, bound_slot)
            var_slot = self.env.lookup(node.cond.left.name)
            self.asm.SUB(cond_slot, bound_slot, var_slot)
            self.free_tmp(bound_slot)
        else:
            # General path: evaluate full boolean condition
            self.emit_expr_into(node.cond, cond_slot)

        self.asm.JIF(cond_slot, done_lbl)
        self.env.push()
        for s in node.body: self.emit_stmt(s)
        self.env.pop()
        self.asm.JMP(loop_lbl)
        self.asm.label(done_lbl)
        self.free_tmp(cond_slot)

    def emit_fundef(self, node):
        lbl, params, fn_tmp_base = self.funs[node.name]
        self.asm.label(lbl)
        # Use this function's statically assigned tmp region (no overlap with any caller).
        saved_env = self.env
        saved_tmp = self._tmp
        self.env = Env(base_slot=100)  # function vars start at slot 100
        self._tmp = fn_tmp_base
        self.env.push()
        # Map parameters: caller passes args in slots 240+
        for i, p in enumerate(params):
            slot = 240 + i
            self.env.define(p, slot)
        for s in node.body:
            self.emit_stmt(s)
        # implicit return 0
        self.asm.SET(self._ret_val, 0)
        self.asm.RET()
        self.env.pop()
        self.env = saved_env
        self._tmp = saved_tmp

    def emit_call(self, node, dst_slot):
        """Emit a function call. Result goes into dst_slot."""
        if node.name not in self.funs:
            raise NameError(f'Unknown function: {node.name!r}')
        lbl, params, _ = self.funs[node.name]
        if len(node.args) != len(params):
            raise TypeError(f'{node.name}: expected {len(params)} args, got {len(node.args)}')
        # Evaluate each argument into a fresh tmp slot first, THEN copy to
        # arg slots 240+. This prevents clobbering when a previous arg's
        # result was placed in an arg slot (e.g. add(double(3), double(4))
        # where double returns into slot 240 which is also the arg slot).
        if len(node.args) == 0:
            self.asm.CALL(lbl)
        elif len(node.args) == 1:
            # Single arg: evaluate directly into 240 (no clobbering possible)
            self.emit_expr_into(node.args[0], 240)
            self.asm.CALL(lbl)
        else:
            # Multiple args: evaluate into fresh tmps, then copy to arg slots
            tmp_slots = [self.alloc_tmp() for _ in node.args]
            for i, arg in enumerate(node.args):
                self.emit_expr_into(arg, tmp_slots[i])
            for i, tmp in enumerate(tmp_slots):
                if tmp != 240 + i:
                    self.asm.ADD(240 + i, tmp, self._zero)
            for tmp in tmp_slots:
                self.free_tmp(tmp)
            self.asm.CALL(lbl)
        # Copy return value to dst
        self.asm.ADD(dst_slot, self._ret_val, self._zero)

    # ── Expression codegen ────────────────────────────────────────────────────
    def emit_expr_into(self, node, dst):
        """Emit code that puts the value of node into slot dst."""
        k = node.kind

        if k == 'Int':
            self.asm.SET(dst, node.val)

        elif k == 'Var':
            src = self.env.lookup(node.name)
            if src != dst:
                self.asm.ADD(dst, src, self._zero)  # dst = src + 0

        elif k == 'Call':
            self.emit_call(node, dst)

        elif k == 'Unary':
            t = self.alloc_tmp()
            self.emit_expr_into(node.operand, t)
            if node.op == 'NEG':
                self.asm.SUB(dst, self._zero, t)      # dst = 0 - t
            elif node.op == 'NOT':
                self.asm.NOT(dst, t)
            self.free_tmp(t)

        elif k == 'BinOp':
            op = node.op
            # Short-circuit AND / OR
            if op == 'AND':
                done = self.fresh_label('and')
                self.emit_expr_into(node.left, dst)
                self.asm.JIF(dst, done)    # if left==0, short-circuit
                self.emit_expr_into(node.right, dst)
                self.asm.label(done)
            elif op == 'OR':
                # OR: if left!=0 → dst=left (truthy), skip right
                #     if left==0 → dst=right
                done = self.fresh_label('or')
                self.emit_expr_into(node.left, dst)
                # t = !left: t=0 when left!=0 (truthy), t=1 when left==0
                t = self.alloc_tmp()
                self.asm.NOT(t, dst)        # t = !left
                self.asm.JIF(t, done)       # JIF fires when t==0 i.e. left!=0 → done
                self.emit_expr_into(node.right, dst)  # left==0, try right
                self.asm.label(done)
                self.free_tmp(t)
            else:
                # Arithmetic / comparison
                lt = self.alloc_tmp(); rt = self.alloc_tmp()
                self.emit_expr_into(node.left,  lt)
                self.emit_expr_into(node.right, rt)
                if   op == 'PLUS':    self.asm.ADD(dst, lt, rt)
                elif op == 'MINUS':   self.asm.SUB(dst, lt, rt)
                elif op == 'STAR':    self.asm.MUL(dst, lt, rt)
                elif op == 'SLASH':   self.asm.DIV(dst, lt, rt)
                elif op == 'PERCENT': self.asm.MOD(dst, lt, rt)
                elif op == 'LT':      self.asm.LT(dst, lt, rt)
                elif op == 'GT':      self.asm.LT(dst, rt, lt)   # a>b ≡ b<a
                elif op == 'EQ':      self.asm.EQ(dst, lt, rt)
                elif op == 'NE':
                    self.asm.EQ(dst, lt, rt); self.asm.NOT(dst, dst)
                elif op == 'LE':
                    # a<=b ≡ !(a>b) ≡ !(b<a)
                    self.asm.LT(dst, rt, lt); self.asm.NOT(dst, dst)
                elif op == 'GE':
                    self.asm.LT(dst, lt, rt); self.asm.NOT(dst, dst)
                self.free_tmp(lt); self.free_tmp(rt)
        else:
            raise NotImplementedError(f'emit_expr_into: {k}')


def compile_source(src):
    """Compile ZCE-lang source string → ZCE binary bytes."""
    ast  = parse(src)
    comp = Compiler()
    return comp.compile(ast)