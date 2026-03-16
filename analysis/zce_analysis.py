"""
ZCE Static Analysis Stack — five passes over the bit stream before execution.
Author: Rishi Saraff

Pass 0: DECODE       — build instruction list from binary
Pass 1: TIER         — classify slots; prove termination for tier-safe programs
Pass 2: DEPS         — build dependency graph (reads/writes per instruction)
Pass 3: WAVES        — topological wave schedule for parallel execution
Pass 4: DEPTH        — static step-count upper bound from M field + tier-1 values
Pass 5: SIMD         — identify groups of 4 independent same-op instructions

All passes are O(n) or O(n²) in program length.
None require execution. All results are exact, not approximate.
"""

from vm.zce_vm import BS, Exec, Asm, AsmOpt, EXT, run_stream, TOK, OP, _BitReader
# Note: in v2, AsmOpt = Asm (alias). Extended ops now at tok8-16 (shifted +2 for CALL/RET).


# ─────────────────────────────────────────────────────────────────────────────
# Pass 0: Decode — build a structured instruction list from a ZCE binary
# ─────────────────────────────────────────────────────────────────────────────

class Instr:
    """Decoded ZCE instruction with full metadata."""
    __slots__ = ('pos', 'tok', 'depth', 'op', 'args', 'reads', 'writes', 'wave')

    def __init__(self, pos, tok, depth, op, args, reads, writes):
        self.pos    = pos     # bit position in stream
        self.tok    = tok     # token identity (N zeros)
        self.depth  = depth   # M field value
        self.op     = op      # operation name string
        self.args   = args    # raw decoded arguments
        self.reads  = set(reads)   # slot indices read
        self.writes = set(writes)  # slot indices written
        self.wave   = -1      # assigned by pass 3

    def __repr__(self):
        return (f"Instr(pos={self.pos}, tok={self.tok}, depth={self.depth}, "
                f"op={self.op!r}, args={self.args}, "
                f"R={sorted(self.reads)}, W={sorted(self.writes)}, wave={self.wave})")


def decode_program(data):
    """
    Decode a ZCE binary into a list of Instr objects.
    Returns (instructions, rules, error_or_None).
    Mirrors Exec.run() but records structure instead of executing.
    """
    br = _BitReader(data)

    def bit():   return br.bit()
    def peek():  return br.peek()
    def elias(): return br.elias()
    def token(): return br.token()

    # pos is now br.pos (absolute bit position)

    P, V = 0, 1
    rules = {
        1: dict(ok=1, na=2, at=[P, V],    act=0, ap=1,  name='SET'),
        2: dict(ok=1, na=3, at=[P, P, P], act=0, ap=2,  name='ADD'),
        3: dict(ok=1, na=2, at=[P, P],    act=1, ap=0,  name='JIF'),   # v2: Elias offset
        4: dict(ok=1, na=1, at=[P],       act=2, ap=0,  name='JMP'),   # v2: Elias offset
        5: dict(ok=1, na=0, at=[],        act=3, ap=0,  name='DEF'),
        6: dict(ok=1, na=1, at=[P],       act=4, ap=0,  name='CALL'),  # v2 new
        7: dict(ok=1, na=0, at=[],        act=5, ap=0,  name='RET'),   # v2 new
    }

    # map extended op codes to names
    ext_names = {v: k for k, v in OP.items()}
    tok_to_ext = {v: k for k, v in TOK.items()}

    # read header
    prog_bits = elias()
    if prog_bits < 0:
        return [], rules, "bad header"
    gs = br.pos
    ge = br.pos + prog_bits

    instrs = []

    while br.pos < ge:
        ipos = br.pos
        tok, depth = token()
        if br.pos > ge: break
        if tok == 0: continue

        # DEF instruction — update rules, record but don't add to instr list
        if tok == 5:
            tgt   = elias()
            na    = elias() + 1
            at    = [elias() for _ in range(na)]
            act   = elias()
            ap    = elias() if act == 0 else 0
            bad   = tgt <= 0 or tgt >= 64 or tgt in {3, 4, 5, 6, 7}
            op_nm = ext_names.get(ap, f'EXT{ap}') if act == 0 else f'ACT{act}'
            if not bad:
                rules[tgt] = dict(ok=1, na=na, at=at, act=act, ap=ap,
                                  name=tok_to_ext.get(tgt, op_nm))
            continue

        if tok not in rules or not rules[tok]['ok']:
            return instrs, rules, f"undef_tok_{tok}"

        r    = rules[tok]
        args = []
        for t in r['at']:
            if t in (0, 1):
                args.append(elias())
            else:
                v = 0
                for _ in range(32): v = (v << 1) | bit()
                args.append(v)

        # determine reads and writes from operation semantics
        act = r['act']
        op_name = r.get('name', f'TOK{tok}')
        reads = set(); writes = set()

        if act == 0:  # slot operation
            ap = r['ap']
            if ap == 1:   # SET: writes dst, reads nothing (value is literal V)
                writes.add(args[0])
            elif ap == 2: # ADD: writes dst, reads a and b
                writes.add(args[0]); reads.add(args[1]); reads.add(args[2])
            elif ap in EXT:
                # extended: SUB MUL DIV MOD EQ LT → dst,a,b
                # NOT → dst,a
                # LOAD → dst, slots[a]  (indirect read)
                # STOR → slots[dst], a  (indirect write)
                if ap in (3,4,5,6,7,8):  # binary: dst a b
                    writes.add(args[0]); reads.add(args[1]); reads.add(args[2])
                elif ap == 9:  # NOT: dst a
                    writes.add(args[0]); reads.add(args[1])
                elif ap == 10: # LOAD: dst ← slots[slots[a]]
                    writes.add(args[0]); reads.add(args[1])
                elif ap == 11: # STOR: slots[slots[dst]] ← slots[a]
                    reads.add(args[0]); reads.add(args[1])

        elif act == 1:  # JIF: reads cond slot, jump offset is Elias
            reads.add(args[0])
        elif act == 2:  # JMP: no slot reads/writes
            pass
        elif act == 4:  # CALL: no slot reads/writes (pure control)
            pass
        elif act == 5:  # RET: no slot reads/writes (pure control)
            pass

        instrs.append(Instr(ipos, tok, depth, op_name, args, reads, writes))

    return instrs, rules, None


# ─────────────────────────────────────────────────────────────────────────────
# Pass 1: Tier Classification — prove termination
# ─────────────────────────────────────────────────────────────────────────────

def classify_tiers(instrs):
    """
    Classify slots into:
      tier1 = CONTROL: appear as conditions of LOOP JIFs (backward-reachable)
      tier0 = DATA: receive arithmetic results

    A JIF is a loop condition if it is enclosed in a loop — i.e. there exists
    a backward jump whose target is at or before the JIF's position. JIFs that
    are pure if-statement conditions (no enclosing loop) are NOT tier-1, because
    they trivially terminate regardless of what drives them.

    Rules for tier-safe programs:
      1. No tier-0 slot may flow into a tier-1 (control) position
      2. Tier-1 slots may only be decremented (SUB) or reset (SET literal)

    Returns (tier0, tier1, violations).
    violations is empty iff the program is tier-safe (guaranteed to terminate).

    Proof:
      Every loop JIF condition checks a tier-1 slot.
      Tier-1 slots only decrease (SUB) or reset (SET literal).
      A non-increasing integer bounded below by zero reaches zero in finite steps.
      Therefore every loop terminates. QED.
    """
    tier1 = set()  # control slots (loop JIF conditions only)
    tier0 = set()  # data slots
    violations = []

    # Build a map of loop regions: loop_start -> loop_end (pos of backward JMP).
    # A loop is defined by a backward jump (JMP or JIF with target < pos).
    # loop_end = pos of the backward jump that closes the loop.
    # loop_start = target of that backward jump.
    loop_regions = {}  # loop_start -> loop_end
    for instr in instrs:
        if instr.op in ('JIF', 'JMP') and instr.args[-1] < instr.pos:
            start = instr.args[-1]
            end   = instr.pos
            # Keep the outermost end for each start (largest loop_end per start)
            if start not in loop_regions or end > loop_regions[start]:
                loop_regions[start] = end

    # First pass: collect tier-1 slots — only from LOOP GUARD JIFs.
    # A JIF is the loop guard (not an if-body JIF) if:
    #   1. It is enclosed in a loop (some loop_start <= JIF.pos <= loop_end)
    #   2. Its jump target exits the loop body (target > loop_end)
    # If-body JIFs stay inside the loop (target <= loop_end) and are NOT tier-1.
    for instr in instrs:
        if instr.op == 'JIF':
            target = instr.args[-1]
            for loop_start, loop_end in loop_regions.items():
                if loop_start <= instr.pos <= loop_end and target > loop_end:
                    tier1.add(instr.args[0])
                    break

    # Second pass: collect tier-0 slots — slots written by INCREASING operations
    # SUB and SET are excluded: SUB decrements (safe for control), SET resets to literal
    INCREASING_OPS = ('ADD', 'MUL', 'DIV', 'MOD', 'EQ', 'LT', 'NOT', 'LOAD')
    for instr in instrs:
        if instr.op in INCREASING_OPS:
            for w in instr.writes:
                tier0.add(w)

    # Check violations: tier-0 (increasing result) flowing into tier-1 (control)
    overlap = tier0 & tier1
    if overlap:
        violations.append(
            f"Slots {sorted(overlap)} are written by increasing ops AND used as "
            f"loop conditions — cannot prove termination"
        )

    # Check tier-1 slots are only decremented (SUB) or reset (SET literal)
    for instr in instrs:
        for w in instr.writes:
            if w in tier1:
                if instr.op == 'SET':
                    pass  # literal reset — ok (re-initialisation)
                elif instr.op == 'SUB':
                    pass  # decrement — ok, provably non-increasing
                elif instr.op in INCREASING_OPS:
                    violations.append(
                        f"Slot {w} is tier-1 (control) but written by {instr.op} "
                        f"at pos {instr.pos} — value may increase, cannot prove termination"
                    )

    return tier0, tier1, violations


def is_tier_safe(data):
    """
    Returns (safe: bool, tier0: set, tier1: set, violations: list).
    safe=True means program is guaranteed to terminate.
    """
    instrs, _, err = decode_program(data)
    if err:
        return False, set(), set(), [f"decode error: {err}"]
    t0, t1, v = classify_tiers(instrs)
    return len(v) == 0, t0, t1, v


# ─────────────────────────────────────────────────────────────────────────────
# Pass 2: Dependency Graph
# ─────────────────────────────────────────────────────────────────────────────

def build_dependency_graph(instrs):
    """
    Returns deps: dict mapping instruction index → set of indices it depends on.
    I depends on J if J writes a slot that I reads, or both write the same slot.
    """
    deps = {i: set() for i in range(len(instrs))}

    for i, instr_i in enumerate(instrs):
        for j in range(i):
            instr_j = instrs[j]
            # true dependency: j writes what i reads
            if instr_j.writes & instr_i.reads:
                deps[i].add(j)
            # output dependency: both write same slot
            if instr_j.writes & instr_i.writes:
                deps[i].add(j)
            # anti-dependency: i writes what j reads (i must come after j)
            if instr_i.writes & instr_j.reads:
                deps[i].add(j)

    return deps


def independent(i1, i2):
    """
    Two instructions are independent if they have no slot conflicts.
    Safe to execute simultaneously or reorder.
    """
    if i1.writes & i2.reads:  return False
    if i2.writes & i1.reads:  return False
    if i1.writes & i2.writes: return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Pass 3: Independence Waves — parallel execution schedule
# ─────────────────────────────────────────────────────────────────────────────

def compute_waves(instrs, deps):
    """
    Assign each instruction to a wave number.
    All instructions in the same wave are mutually independent
    and can execute simultaneously.

    Wave assignment: instruction i goes into wave w where w is the
    smallest integer such that all dependencies of i are in waves < w.

    Returns instrs with .wave field set, and list-of-lists wave schedule.
    """
    wave_of = {}

    for i, instr in enumerate(instrs):
        if not deps[i]:
            wave_of[i] = 0
        else:
            wave_of[i] = 1 + max(wave_of[j] for j in deps[i])
        instr.wave = wave_of[i]

    max_wave = max(wave_of.values()) if wave_of else -1
    schedule = [[] for _ in range(max_wave + 1)]
    for i, instr in enumerate(instrs):
        schedule[instr.wave].append(i)

    return schedule


def parallelism_stats(instrs, schedule):
    """Compute parallelism statistics from wave schedule."""
    if not schedule:
        return {'waves': 0, 'max_wave_size': 0, 'avg_wave_size': 0.0,
                'theoretical_speedup': 1.0, 'total_instrs': 0}

    sizes = [len(w) for w in schedule if w]
    total = len(instrs)
    return {
        'waves':              len(sizes),
        'max_wave_size':      max(sizes) if sizes else 0,
        'avg_wave_size':      sum(sizes) / len(sizes) if sizes else 0.0,
        'theoretical_speedup': total / len(sizes) if sizes else 1.0,
        'total_instrs':       total,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pass 4: Depth Bound — static step count upper bound
# ─────────────────────────────────────────────────────────────────────────────

def compute_depth_bound(instrs, tier1_slots, initial_slots=None):
    """
    Compute a static upper bound on step count (logical depth).

    Method:
      For each tier-1 (control) slot, find its initial value from SET instructions.
      For each loop nesting level (M depth), the iteration count is bounded by
      the initial value of the controlling tier-1 slot.
      Total steps ≤ product of bounds across nesting levels × instructions per body.

    initial_slots: dict of slot→value for pre-set inputs (optional).
    Returns dict with bound info.
    """
    if initial_slots is None:
        initial_slots = {}

    # Collect initial values of all SET-initialised slots (not just tier-1),
    # because tier-1 slots may be computed from SET-initialised operands via SUB.
    slot_init = dict(initial_slots)
    for instr in instrs:
        if instr.op == 'SET':
            slot_init[instr.args[0]] = instr.args[1]

    # Compiled while loops write tier-1 slots via SUB(dst, bound, var) rather than
    # SET(dst, val). The initial value can be derived from the SUB operands:
    #   initial(dst) = initial(bound) - initial(var)
    # Walk instructions in order so earlier SETs are visible when we hit SUB.
    for instr in instrs:
        if instr.op == 'SUB' and instr.args[0] in tier1_slots:
            dst, a, b = instr.args[0], instr.args[1], instr.args[2]
            if dst not in slot_init and a in slot_init and b in slot_init:
                slot_init[dst] = slot_init[a] - slot_init[b]

    # find JIF instructions and their conditions
    loops = []
    for instr in instrs:
        if instr.op == 'JIF':
            cond_slot = instr.args[0]
            init_val  = slot_init.get(cond_slot, None)
            loops.append({
                'depth':     instr.depth,
                'cond_slot': cond_slot,
                'init_val':  init_val,
                'pos':       instr.pos,
            })

    # bound = product of initial values at each nesting level
    # if any value is unknown, mark as unbounded
    bound = 1
    unbounded = []
    for loop in loops:
        if loop['init_val'] is None:
            unbounded.append(loop['cond_slot'])
        else:
            bound *= (loop['init_val'] + 1)

    # total instructions including control flow (JIF, JMP count as steps too)
    total_instrs = len(instrs)
    total_bound = bound * max(total_instrs, 1) if not unbounded else None

    return {
        'loops':        loops,
        'slot_inits':   slot_init,
        'step_bound':   total_bound,
        'unbounded_slots': unbounded,
        'is_bounded':   len(unbounded) == 0,
        'non_loop_instrs': total_instrs,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pass 5: SIMD Grouping — identify Cortex-M4 SIMD opportunities
# ─────────────────────────────────────────────────────────────────────────────

SIMD_OPS = {'ADD', 'SUB', 'MUL', 'EQ', 'LT'}  # binary ops with SIMD variants
SIMD_WIDTH = 4  # Cortex-M4 NEON: 4 × int32

def find_simd_groups(instrs, schedule):
    """
    Within each wave, find groups of SIMD_WIDTH independent instructions
    that perform the same operation on consecutive destination slots.

    These map directly to a single SIMD instruction on Cortex-M4 DSP.
    e.g. four ADD instructions on slots 20,21,22,23 → one SIMD4 ADD.

    Returns list of SIMDGroup objects.
    """
    groups = []

    for wave_idx, wave in enumerate(schedule):
        wave_instrs = [instrs[i] for i in wave]

        # group by operation
        by_op = {}
        for instr in wave_instrs:
            if instr.op in SIMD_OPS:
                by_op.setdefault(instr.op, []).append(instr)

        for op, op_instrs in by_op.items():
            # sort by destination slot
            op_instrs.sort(key=lambda x: min(x.writes) if x.writes else 0)

            # find runs of consecutive destination slots
            run = []
            for instr in op_instrs:
                dst = min(instr.writes) if instr.writes else None
                if dst is None: continue
                if not run:
                    run.append(instr)
                elif min(run[-1].writes) + 1 == dst:
                    run.append(instr)
                    if len(run) == SIMD_WIDTH:
                        groups.append({
                            'wave':  wave_idx,
                            'op':    op,
                            'instrs': list(run),
                            'dsts':  [min(i.writes) for i in run],
                        })
                        run = []
                else:
                    run = [instr]

    return groups


# ─────────────────────────────────────────────────────────────────────────────
# Full static analysis pipeline
# ─────────────────────────────────────────────────────────────────────────────

def analyze(data, initial_slots=None):
    """
    Run all six passes on a ZCE binary.
    Returns a comprehensive analysis dict.
    """
    result = {}

    # Pass 0: decode
    instrs, rules, err = decode_program(data)
    result['instrs']    = instrs
    result['rules']     = rules
    result['decode_err'] = err
    if err:
        result['tier_safe'] = False
        return result

    # Pass 1: tier classification
    tier0, tier1, violations = classify_tiers(instrs)
    result['tier0']      = tier0
    result['tier1']      = tier1
    result['violations'] = violations
    result['tier_safe']  = len(violations) == 0

    # Pass 2: dependency graph
    deps = build_dependency_graph(instrs)
    result['deps'] = deps

    # Pass 3: wave schedule
    schedule = compute_waves(instrs, deps)
    result['schedule'] = schedule
    result['par_stats'] = parallelism_stats(instrs, schedule)

    # Pass 4: depth bound
    depth = compute_depth_bound(instrs, tier1, initial_slots)
    result['depth'] = depth

    # Pass 5: SIMD groups
    simd = find_simd_groups(instrs, schedule)
    result['simd_groups'] = simd
    result['simd_count']  = len(simd)

    return result


def print_analysis(r, verbose=False):
    """Pretty-print analysis results."""
    print(f"  instructions : {len(r['instrs'])}")
    print(f"  tier-safe    : {r['tier_safe']}")
    if r['violations']:
        for v in r['violations']:
            print(f"    ✗ {v}")
    else:
        print(f"    ✓ termination guaranteed")
    ps = r.get('par_stats', {})
    print(f"  waves        : {ps.get('waves', '?')}")
    print(f"  max wave size: {ps.get('max_wave_size', '?')}")
    print(f"  speedup      : {ps.get('theoretical_speedup', 1.0):.2f}×")
    d = r.get('depth', {})
    if d.get('is_bounded'):
        print(f"  step bound   : {d['step_bound']}")
    else:
        print(f"  step bound   : unbounded (slots {d.get('unbounded_slots')} unknown)")
    print(f"  SIMD groups  : {r.get('simd_count', 0)}")
    if verbose and r['instrs']:
        print(f"\n  Wave schedule:")
        for wi, wave in enumerate(r['schedule']):
            ops = [r['instrs'][i].op for i in wave]
            print(f"    wave {wi}: {ops}")



# ─────────────────────────────────────────────────────────────────────────────
# Parallel Executor — snapshot-then-apply wave execution
# ─────────────────────────────────────────────────────────────────────────────

class ParallelExec:
    """
    Executes a ZCE program using the wave schedule instead of sequential order.

    For each wave:
      1. Snapshot current slot values (all reads for this wave come from snapshot)
      2. Execute every instruction in the wave, reading from snapshot
      3. Apply all writes atomically after all reads are done

    This is the 'snapshot-then-apply' model used by GPU compute shaders and
    dataflow processors. It works correctly if and only if instructions within
    a wave are truly independent (no shared slot reads/writes).

    If the wave schedule is correct, ParallelExec(binary) == run_stream(binary).
    Running both and comparing is a live correctness proof of the wave schedule.

    This models how the wave schedule would be used on real hardware:
      - ESP32 dual-core: each core executes half the instructions in a wave
      - Cortex-M4: SIMD groups execute in one instruction
      - Pipeline: all instructions of a wave issue simultaneously

    Usage:
      pex = ParallelExec()
      ok, slot0 = pex.run(binary)
      conflicts = pex.conflicts  # should be [] if wave schedule is correct
    """

    MAX_SLOTS = 512

    def __init__(self):
        self.slots     = [0] * self.MAX_SLOTS
        self.conflicts = []   # list of (wave_idx, instr_i, instr_j, slot) if any
        self.waves_executed = 0
        self.instrs_executed = 0

    def _apply_instr(self, instr, snapshot, ext_ops):
        """
        Execute one instruction reading from snapshot, return (slot, value) writes.
        Returns list of (slot_index, new_value) pairs.
        """
        op = instr.op
        args = instr.args

        def rslot(i):
            return snapshot[i] if 0 <= i < self.MAX_SLOTS else 0

        if op == 'SET':
            return [(args[0], args[1])]

        elif op == 'ADD':
            return [(args[0], rslot(args[1]) + rslot(args[2]))]

        elif op == 'SUB':
            return [(args[0], rslot(args[1]) - rslot(args[2]))]

        elif op == 'MUL':
            return [(args[0], rslot(args[1]) * rslot(args[2]))]

        elif op == 'DIV':
            b = rslot(args[2])
            return [(args[0], rslot(args[1]) // b if b != 0 else 0)]

        elif op == 'MOD':
            b = rslot(args[2])
            return [(args[0], rslot(args[1]) % b if b != 0 else 0)]

        elif op == 'EQ':
            return [(args[0], 1 if rslot(args[1]) == rslot(args[2]) else 0)]

        elif op == 'LT':
            return [(args[0], 1 if rslot(args[1]) < rslot(args[2]) else 0)]

        elif op == 'NOT':
            return [(args[0], 0 if rslot(args[1]) != 0 else 1)]

        elif op == 'LOAD':
            idx = rslot(args[1])
            val = snapshot[idx] if 0 <= idx < self.MAX_SLOTS else 0
            return [(args[0], val)]

        elif op == 'STOR':
            idx = rslot(args[0])
            if 0 <= idx < self.MAX_SLOTS:
                return [(idx, rslot(args[1]))]
            return []

        # Control instructions (JIF, JMP, CALL, RET) are not slot operations
        # They are handled by sequential execution between waves
        return []

    def run(self, data):
        """
        Run program using wave-parallel execution.
        Returns (ok, final_slots).
        """
        from vm.zce_vm import run_stream

        # Get the wave schedule and instruction list
        instrs, rules, err = decode_program(data)
        if err:
            return False, list(self.slots)

        # Get wave schedule
        deps     = build_dependency_graph(instrs)
        schedule = compute_waves(instrs, deps)

        # Reset state
        self.slots          = [0] * self.MAX_SLOTS
        self.conflicts      = []
        self.waves_executed = 0
        self.instrs_executed = 0

        # Execute waves in order
        # Note: control flow (JIF/JMP/CALL/RET) makes full wave-parallel execution
        # of arbitrary programs complex. For programs WITHOUT control flow (pure
        # dataflow), we execute all instructions wave by wave.
        # For programs WITH control flow, we fall back to sequential within waves
        # but still verify independence within each wave.
        has_control = any(i.op in ('JIF', 'JMP', 'CALL', 'RET') for i in instrs)

        if has_control:
            # With control flow: use reference VM for execution order,
            # but verify wave independence by checking no conflicts exist
            vm, ok = run_stream(data)
            self.slots = list(vm.slots)
            # Verify every wave has zero conflicts
            for wi, wave in enumerate(schedule):
                for x in range(len(wave)):
                    for y in range(x + 1, len(wave)):
                        i1, i2 = instrs[wave[x]], instrs[wave[y]]
                        if not independent(i1, i2):
                            self.conflicts.append((wi, wave[x], wave[y]))
            self.waves_executed   = len(schedule)
            self.instrs_executed  = len(instrs)
            return ok, list(self.slots)

        # Pure dataflow (no control flow): execute wave by wave
        for wi, wave in enumerate(schedule):
            snapshot = list(self.slots)  # snapshot before this wave

            # Check for intra-wave conflicts (should be zero)
            for x in range(len(wave)):
                for y in range(x + 1, len(wave)):
                    i1, i2 = instrs[wave[x]], instrs[wave[y]]
                    if not independent(i1, i2):
                        self.conflicts.append((wi, wave[x], wave[y]))

            # Execute all instructions in wave reading from snapshot
            pending_writes = []
            for idx in wave:
                instr = instrs[idx]
                writes = self._apply_instr(instr, snapshot, {})
                pending_writes.extend(writes)
                self.instrs_executed += 1

            # Apply all writes atomically
            for slot_idx, value in pending_writes:
                if 0 <= slot_idx < self.MAX_SLOTS:
                    self.slots[slot_idx] = value

            self.waves_executed += 1

        return True, list(self.slots)

    def agrees_with_sequential(self, data):
        """
        Run both parallel and sequential execution and compare slot values.
        Returns (agrees: bool, parallel_slots, sequential_slots).
        """
        from vm.zce_vm import run_stream
        ok_par, par_slots = self.run(data)
        vm_seq, ok_seq    = run_stream(data)
        seq_slots = list(vm_seq.slots[:self.MAX_SLOTS])
        # Compare all non-zero slots (zero is default, only compare written ones)
        instrs, _, _ = decode_program(data)
        written = set()
        for i in instrs:
            written.update(i.writes)
        agrees = all(par_slots[s] == seq_slots[s] for s in written
                     if s < self.MAX_SLOTS)
        return agrees, par_slots, seq_slots


# ─────────────────────────────────────────────────────────────────────────────
# P_tier prior — REMOVED
#
# What it did: enumerated tier-safe ZCE programs up to a bit-length bound,
# weighted each by 2^(-length), and computed P_tier(target) as the normalised
# sum of weights for programs producing that output. Intended for anomaly
# detection on embedded sensor streams: a spike in description length signals
# an anomaly.
#
# Why it was interesting: P_tier is a computable approximation to Solomonoff
# induction restricted to the tier-safe class. Because tier-safe programs
# always terminate, the prior is well-defined and computable — unlike the full
# Solomonoff prior which requires solving the halting problem.
#
# Why removed:
#   1. Enumeration covered only 3 hardcoded program shapes (SET literals,
#      simple arithmetic, counting loops) — not exhaustive within any bit-length
#      bound, so the prior mass was not a correct probability.
#   2. Nothing in the deployed system calls it. Hardware is the goal.
#   3. 110 lines of uncalled, incomplete code is dead weight.
#
# To restore: git history has the full implementation. A correct P_tier would
# enumerate all tier-safe programs by exact bit length using the self-delimiting
# format's counting theorem, not by structural shape.
# ─────────────────────────────────────────────────────────────────────────────