"""
ZCE Streaming Executor
Author: Rishi Saraff

RAM model:
  Reference executor:  O(program_bits) — loads everything as Python ints
  Streaming executor:  O(slots + max_loop_body) — independent of program size

Supports all v2 instructions including CALL/RET.
"""

from vm.zce_vm import EXT


class ByteStream:
    """O(1) RAM bit reader."""
    def __init__(self, data):
        self._data = data
        self._byte_pos = 0; self._bit_pos = 0; self._abs_bit = 0
        self._cur_byte = data[0] if data else 0
        self._at_end = len(data) == 0

    def read_bit(self):
        if self._at_end: return -1
        bit = (self._cur_byte >> (7 - self._bit_pos)) & 1
        self._abs_bit += 1; self._bit_pos += 1
        if self._bit_pos == 8:
            self._bit_pos = 0; self._byte_pos += 1
            if self._byte_pos < len(self._data):
                self._cur_byte = self._data[self._byte_pos]
            else:
                self._at_end = True
        return bit

    def peek_bit(self):
        if self._at_end: return -1
        return (self._cur_byte >> (7 - self._bit_pos)) & 1

    def tell(self): return self._abs_bit

    def seek(self, abs_bit):
        self._byte_pos = abs_bit // 8; self._bit_pos = abs_bit % 8
        self._abs_bit = abs_bit; self._at_end = False
        if self._byte_pos < len(self._data):
            self._cur_byte = self._data[self._byte_pos]
        else:
            self._at_end = True


class BitBuffer:
    """Reads from a captured list of bits. Used for loop re-execution."""
    def __init__(self, bits):
        self._bits = bits; self._pos = 0
    def read_bit(self):
        if self._pos >= len(self._bits): return -1
        b = self._bits[self._pos]; self._pos += 1; return b
    def peek_bit(self):
        if self._pos >= len(self._bits): return -1
        return self._bits[self._pos]
    def tell(self): return self._pos
    def seek(self, pos): self._pos = max(0, min(pos, len(self._bits)))
    def __len__(self): return len(self._bits)


class StreamExec:
    """
    Streaming ZCE executor.
    RAM = O(slots + max_loop_body), independent of program size.
    Supports all v2 instructions including CALL/RET.
    """
    MAX_SLOTS = 512   # must exceed compiler's highest tmp slot (230 + 20*(n_fns+1))
    MAX_STEPS = 50_000_000

    def __init__(self, ext=None):
        self.slots = [0] * self.MAX_SLOTS
        self.rules = {}
        self.ext = ext or {}
        self.err = None
        self.steps = 0
        self.depth_log = []
        self.instructions_executed = 0
        self.loop_iterations = 0
        self.peak_loop_bytes = 0
        self._loop_buffers = {}
        self._reader = None
        self._gs = 0; self._ge = 0; self._data = None
        self._call_stack = []

        P, V = 0, 1
        self.rules[1] = dict(ok=1, na=2, at=[P,V],   act=0, ap=1)
        self.rules[2] = dict(ok=1, na=3, at=[P,P,P], act=0, ap=2)
        self.rules[3] = dict(ok=1, na=2, at=[P,P],   act=1, ap=0)
        self.rules[4] = dict(ok=1, na=1, at=[P],     act=2, ap=0)
        self.rules[5] = dict(ok=1, na=0, at=[],      act=3, ap=0)
        self.rules[6] = dict(ok=1, na=1, at=[P],     act=4, ap=0)  # CALL
        self.rules[7] = dict(ok=1, na=0, at=[],      act=5, ap=0)  # RET

    def _bit(self):
        b = self._reader.read_bit()
        if b == -1: self.err = "eof"; return 0
        return b

    def _peek(self): return self._reader.peek_bit()

    def _elias(self):
        k = 0
        while self._peek() == 0:
            self._bit(); k += 1
            if k > 30: self.err = "elias_ovf"; return -1
        x = 0
        for _ in range(k+1): x = (x<<1) | self._bit()
        return x - 1

    def _token(self):
        n = 0
        while self._peek() == 0: self._bit(); n += 1
        m = 0
        while self._peek() == 1: self._bit(); m += 1
        self._bit()
        return n, m - 1

    def _arg(self, t):
        if t in (0, 1): return self._elias()
        v = 0
        for _ in range(32): v = (v<<1) | self._bit()
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

    def _get(self, i): return self.slots[i] if 0 <= i < self.MAX_SLOTS else 0
    def _set(self, i, v):
        if 0 <= i < self.MAX_SLOTS: self.slots[i] = v

    def _exec_one(self):
        """Execute one instruction. Returns (action, target)."""
        pos = self._reader.tell()
        tok, depth = self._token()
        if self.err: return 'error', 0
        if tok == 0: return 'continue', 0
        if tok == 5: self._define(); return 'continue', 0

        # CALL — push return address, jump to subroutine
        if tok == 6:
            off = self._elias()
            if self.err: return 'error', 0
            if len(self._call_stack) >= 64:
                self.err = 'call_stack_overflow'; return 'error', 0
            self._call_stack.append(self._reader.tell())
            target = self._gs + off
            self.steps += 1
            return 'jump_fwd', target

        # RET — pop return address
        if tok == 7:
            if not self._call_stack:
                self.err = 'ret_without_call'; return 'error', 0
            ret_addr = self._call_stack.pop()
            self.steps += 1
            return 'jump_fwd', ret_addr

        if tok not in self.rules or not self.rules[tok]['ok']:
            self.err = f"undef_tok_{tok}"; return 'error', 0

        r = self.rules[tok]
        args = [self._arg(r['at'][i]) for i in range(r['na'])]
        if self.err: return 'error', 0

        self.depth_log.append((tok, depth))
        self.instructions_executed += 1
        self.steps += 1
        act = r['act']

        if act == 0:
            p = r['ap']
            if   p == 1: self._set(args[0], args[1])
            elif p == 2: self._set(args[0], self._get(args[1]) + self._get(args[2]))
            elif p in self.ext: self.ext[p](self.slots, args)
            return 'continue', 0

        elif act == 1:  # JIF
            if self._get(args[0]) == 0:
                target = self._gs + args[1]
                if target < pos: return 'jump_back', target
                else: return 'jump_fwd', target
            return 'continue', 0

        elif act == 2:  # JMP
            target = self._gs + args[0]
            if target < pos: return 'jump_back', target
            else: return 'jump_fwd', target

        return 'continue', 0

    def run(self, data):
        self._data = data
        self._reader = ByteStream(data)
        self._call_stack = []
        gb = self._elias()
        if self.err: return False
        self._gs = self._reader.tell()
        self._ge = self._gs + gb
        self._run_main()
        return not bool(self.err)

    def _run_main(self):
        """Run main stream until program end."""
        while not self.err and self.steps < self.MAX_STEPS:
            if self._reader.tell() >= self._ge: break
            action, target = self._exec_one()
            if action == 'error': break
            elif action == 'jump_fwd':
                if target >= self._ge: break
                self._reader.seek(target)
            elif action == 'jump_back':
                self._run_loop(target)

    def _run_until_return(self):
        """Run on main stream until call stack depth drops below entry level."""
        depth_at_entry = len(self._call_stack)
        while not self.err and self.steps < self.MAX_STEPS:
            if self._reader.tell() >= self._ge: break
            action, target = self._exec_one()
            if action == 'error': break
            elif action == 'jump_fwd':
                if target >= self._ge: break
                self._reader.seek(target)
            elif action == 'jump_back':
                self._run_loop(target)
            if len(self._call_stack) < depth_at_entry:
                break

    def _run_loop(self, loop_start_abs):
        """
        Execute a loop whose body starts at loop_start_abs.
        Buffer the body on first encounter; re-use on subsequent iterations.
        CALL instructions inside the loop body are dispatched to _run_until_return
        on the main stream, then execution resumes in the buffer.
        """
        loop_end_abs = self._reader.tell()

        if loop_start_abs not in self._loop_buffers:
            body_len = loop_end_abs - loop_start_abs
            tmp = ByteStream(self._data)
            tmp.seek(loop_start_abs)
            bits = [tmp.read_bit() for _ in range(body_len)]
            self._loop_buffers[loop_start_abs] = bits
            buf_bytes = sum(len(b) for b in self._loop_buffers.values()) // 8
            self.peak_loop_bytes = max(self.peak_loop_bytes, buf_bytes)

        buf_bits = self._loop_buffers[loop_start_abs]
        buf_len  = len(buf_bits)

        while not self.err and self.steps < self.MAX_STEPS:
            self.loop_iterations += 1
            buf = BitBuffer(buf_bits)
            self._reader = buf
            exited = False

            while not self.err and self.steps < self.MAX_STEPS:
                if buf.tell() >= buf_len:
                    break  # ran off end — restart loop

                action, target = self._exec_one()
                if action == 'error': return

                elif action == 'jump_fwd':
                    target_offset = target - loop_start_abs
                    if target_offset >= buf_len:
                        # Forward jump past buffer end.
                        # If call stack grew, this is a CALL to a subroutine
                        # outside the loop — run it on the main stream then resume.
                        if self._call_stack and self._call_stack[-1] > loop_start_abs:
                            resume_abs = self._call_stack[-1]
                            resume_offset = resume_abs - loop_start_abs
                            self._reader = ByteStream(self._data)
                            self._reader.seek(target)
                            self._run_until_return()
                            if self.err: return
                            buf.seek(resume_offset)
                            self._reader = buf
                        else:
                            # Genuine loop exit
                            self._reader = ByteStream(self._data)
                            tgt = target if target < self._ge else self._ge
                            self._reader.seek(tgt)
                            exited = True
                            break
                    else:
                        buf.seek(target_offset)

                elif action == 'jump_back':
                    target_offset = target - loop_start_abs
                    if target_offset <= 0:
                        break  # restart from beginning
                    else:
                        buf.seek(target_offset)

            if exited:
                return

            # Loop restarted — switch reader back temporarily then continue
            self._reader = ByteStream(self._data)
            continue

    def slot(self, i): return self._get(i)

    def ram_report(self):
        slot_ram  = self.MAX_SLOTS * 4  # int32 = 4 bytes
        rules_ram = len(self.rules) * 64
        loop_ram  = sum(len(b) for b in self._loop_buffers.values()) // 8 + 1
        misc_ram  = 256
        total = slot_ram + rules_ram + loop_ram + misc_ram
        return {
            'slot_array_bytes':  slot_ram,
            'rules_bytes':       rules_ram,
            'loop_buffer_bytes': loop_ram,
            'misc_bytes':        misc_ram,
            'total_bytes':       total,
            'total_kb':          round(total / 1024, 2),
            'peak_loop_bytes':   self.peak_loop_bytes,
            'loop_count':        len(self._loop_buffers),
        }


def predict_ram(data):
    """Static RAM prediction: scan bit stream, find largest loop body."""
    stream = ByteStream(data)

    def read_elias():
        k = 0
        while stream.peek_bit() == 0: stream.read_bit(); k += 1
        x = 0
        for _ in range(k+1): x = (x<<1) | stream.read_bit()
        return x - 1

    gb = read_elias()
    gs = stream.tell(); ge = gs + gb
    max_loop_bits = 0; backwards_jumps = []
    arities = {1:2,2:3,3:2,4:1,5:0,6:1,7:0,
               8:3,9:3,10:3,11:3,12:3,13:3,14:2,15:2,16:2}

    while stream.tell() < ge:
        pos = stream.tell()
        n = 0
        while stream.peek_bit() == 0: stream.read_bit(); n += 1
        m = 0
        while stream.peek_bit() == 1: stream.read_bit(); m += 1
        stream.read_bit()
        if n == 0: continue
        na = arities.get(n, 2)
        args = [read_elias() for _ in range(na)]
        if n == 3 and len(args) >= 2:
            target = gs + args[1]
            if target < pos:
                body = pos - target; max_loop_bits = max(max_loop_bits, body)
                backwards_jumps.append(body)
        elif n == 4 and len(args) >= 1:
            target = gs + args[0]
            if target < pos:
                body = pos - target; max_loop_bits = max(max_loop_bits, body)
                backwards_jumps.append(body)

    slot_ram  = 512 * 4   # matches StreamExec.MAX_SLOTS * int32
    rules_ram = 20 * 64
    loop_ram  = max_loop_bits // 8 + 1
    misc_ram  = 256
    total = slot_ram + rules_ram + loop_ram + misc_ram
    return {
        'predicted_bytes': total,
        'predicted_kb':    round(total / 1024, 2),
        'max_loop_bits':   max_loop_bits,
        'max_loop_bytes':  max_loop_bits // 8,
        'backwards_jumps': len(backwards_jumps),
        'breakdown': {
            'slots': slot_ram, 'rules': rules_ram,
            'loops': loop_ram, 'misc':  misc_ram,
        }
    }


def run_streaming(data):
    ex = StreamExec(ext=EXT)
    ok = ex.run(data)
    return ex, ok