"""
ZCE Parallel C Executor Generator
Author: Rishi Saraff

Generates C source code from a ZCE binary that executes the program
using the wave schedule from static analysis.

Two execution modes:
  Sequential:  waves execute one after another (baseline, always correct)
  Parallel:    waves execute with OpenMP (real parallelism on multi-core)

The generated C is self-contained — no ZCE VM header needed.
It encodes the wave schedule as static C arrays and executes them directly.

This is "ahead-of-time parallel scheduling":
  Developer: run analyze(binary) -> wave schedule (Python, developer machine)
  Generator: emit C code encoding the schedule (this module)
  Hardware:  compile and run the C (no analysis at runtime, just execute)

The schedule is proven correct by the wave independence proof.
The C code just executes it — no locks, no race conditions, no hazards.

Usage:
    from analysis.zce_parallel_c import generate_parallel_c, compile_and_run

    binary = compile_source(src)
    c_src  = generate_parallel_c(binary, parallel=True)
    result = compile_and_run(c_src, expected_slot=0)
"""

import subprocess
import tempfile
import os

# Import analysis tools
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from analysis.zce_analysis import (
    analyze, decode_program, build_dependency_graph,
    compute_waves, independent, Instr
)
from vm.zce_vm import EXT


# ── Instruction execution as C expression ────────────────────────────────────

def _instr_to_c(instr, snap='snap', slots='slots'):
    """
    Emit C statement(s) for one instruction.
    Reads from snap[], writes to slots[].
    snap and slots may be the same array (sequential) or different (parallel).
    """
    op   = instr.op
    args = instr.args

    def r(i):
        """Read slot i from snapshot."""
        if 0 <= i < 512:
            return f'{snap}[{i}]'
        return '0'

    def w(i, expr):
        """Write expr to slot i in output slots."""
        if 0 <= i < 512:
            return f'    {slots}[{i}] = {expr};'
        return f'    /* slot {i} out of range */'

    if op == 'SET':
        return w(args[0], str(args[1]))
    elif op == 'ADD':
        return w(args[0], f'{r(args[1])} + {r(args[2])}')
    elif op == 'SUB':
        return w(args[0], f'{r(args[1])} - {r(args[2])}')
    elif op == 'MUL':
        return w(args[0], f'{r(args[1])} * {r(args[2])}')
    elif op == 'DIV':
        return w(args[0], f'({r(args[2])} != 0) ? {r(args[1])} / {r(args[2])} : 0')
    elif op == 'MOD':
        return w(args[0], f'({r(args[2])} != 0) ? {r(args[1])} % {r(args[2])} : 0')
    elif op == 'EQ':
        return w(args[0], f'({r(args[1])} == {r(args[2])}) ? 1 : 0')
    elif op == 'LT':
        return w(args[0], f'({r(args[1])} < {r(args[2])}) ? 1 : 0')
    elif op == 'NOT':
        return w(args[0], f'({r(args[1])} != 0) ? 0 : 1')
    elif op == 'LOAD':
        idx = f'{r(args[1])}'
        return w(args[0], f'(({idx}) >= 0 && ({idx}) < 512) ? {snap}[{idx}] : 0')
    elif op == 'STOR':
        idx = f'{r(args[0])}'
        return f'    if (({idx}) >= 0 && ({idx}) < 512) {slots}[{idx}] = {r(args[1])};'
    elif op in ('JIF', 'JMP', 'CALL', 'RET', 'DEF'):
        return f'    /* {op} handled by control flow */'
    else:
        return f'    /* unknown op {op} */'


def generate_parallel_c(binary, parallel=True, max_slots=512, label='zce_prog'):
    """
    Generate C source that executes the ZCE binary using the wave schedule.

    Parameters:
        binary:     ZCE binary bytes
        parallel:   True = use OpenMP for parallel waves (requires -fopenmp)
                    False = sequential wave execution (baseline)
        max_slots:  slot array size
        label:      C function name prefix

    Returns:
        Complete C source string. Compile with:
            gcc -O2 [-fopenmp] -o prog prog.c
        or seekable embedded:
            gcc -O2 -DZCE_SEEKABLE -fopenmp -o prog prog.c
    """
    from vm.zce_vm import run_stream

    instrs, rules, err = decode_program(binary)
    if err:
        raise ValueError(f'decode error: {err}')

    deps     = build_dependency_graph(instrs)
    schedule = compute_waves(instrs, deps)
    r        = analyze(binary)
    ps       = r['par_stats']

    # Separate control-flow instructions from data instructions
    CONTROL_OPS = {'JIF', 'JMP', 'CALL', 'RET'}
    DATA_OPS    = {'SET', 'ADD', 'SUB', 'MUL', 'DIV', 'MOD',
                   'EQ', 'LT', 'NOT', 'LOAD', 'STOR'}

    has_control = any(i.op in CONTROL_OPS for i in instrs)

    lines = []

    # Header
    lines += [
        f'/* ZCE Generated {"Parallel" if parallel else "Sequential"} Executor',
        f' * Program: {label}',
        f' * Instructions: {len(instrs)}',
        f' * Waves: {ps["waves"]}  Theoretical speedup: {ps["theoretical_speedup"]:.2f}x',
        f' * {"OpenMP parallel" if parallel else "Sequential"} execution',
        f' * Compile: gcc -O2{" -fopenmp" if parallel else ""} -o {label} {label}.c',
        f' */',
        '',
        '#include <stdint.h>',
        '#include <stdio.h>',
        '#include <string.h>',
        '#include <time.h>',
    ]
    if parallel:
        lines.append('#include <omp.h>')

    lines += [
        '',
        f'#define MAX_SLOTS {max_slots}',
        f'static int32_t slots[MAX_SLOTS];',
        '',
    ]

    if not has_control:
        # Pure dataflow: full wave-parallel execution
        lines += _gen_dataflow_executor(instrs, schedule, parallel, label)
    else:
        # Control flow: use reference execution but generate wave-annotated version
        # For looping programs, we emit the wave schedule as a reference executor
        # that runs the wave in sequence but shows where parallelism is possible
        lines += _gen_control_executor(binary, instrs, schedule, parallel, label, max_slots)

    # Main: run, time, print result
    lines += [
        '',
        'int main(void) {',
        '    clock_t t0, t1;',
        f'    t0 = clock();',
        f'    int result = {label}_run();',
        '    t1 = clock();',
        '    double ms = (double)(t1-t0)/CLOCKS_PER_SEC*1000.0;',
        f'    printf("{label}: result=%d  time=%.4fms\\n", result, ms);',
        '    return 0;',
        '}',
    ]

    return '\n'.join(lines)


def _gen_dataflow_executor(instrs, schedule, parallel, label):
    """Generate C for pure dataflow (no control flow) programs."""
    lines = [
        f'int {label}_run(void) {{',
        f'    int32_t snap[MAX_SLOTS];',
        '',
    ]

    for wi, wave in enumerate(schedule):
        wave_instrs = [instrs[i] for i in wave]
        data_instrs = [i for i in wave_instrs if i.op not in ('JIF','JMP','CALL','RET')]

        lines.append(f'    /* Wave {wi}: {len(data_instrs)} instructions */')

        if parallel and len(data_instrs) > 1:
            # Snapshot before wave
            lines.append('    memcpy(snap, slots, sizeof(slots));')
            # Check for SIMD opportunity (4+ consecutive same-op on consecutive slots)
            simd_ranges = _find_simd_ranges(data_instrs)
            emitted = set()

            lines.append(f'    #pragma omp parallel for schedule(static) num_threads(4)')
            lines.append(f'    for (int _w{wi}=0; _w{wi}<{len(data_instrs)}; _w{wi}++) {{')
            lines.append(f'        switch(_w{wi}) {{')
            for idx, instr in enumerate(data_instrs):
                c = _instr_to_c(instr, snap='snap', slots='slots').strip()
                lines.append(f'        case {idx}: {c} break;')
            lines.append('        }')
            lines.append('    }')
        else:
            # Sequential: read and write slots directly
            for instr in data_instrs:
                lines.append(_instr_to_c(instr, snap='slots', slots='slots'))

        lines.append('')

    lines += [
        '    return (int)slots[0];',
        '}',
    ]
    return lines


def _gen_control_executor(binary, instrs, schedule, parallel, label, max_slots):
    """
    Generate C for programs with control flow.
    Uses the ZCE C VM header for execution, but annotates with wave schedule.
    The parallel version uses OpenMP on the wave schedule annotations.
    """
    # Embed binary as C array
    arr = ', '.join(str(b) for b in binary)
    lines = [
        '/* Uses ZCE VM for control flow + wave schedule for parallelism info */',
        f'static const uint8_t _prog[] = {{{arr}}};',
        '',
    ]

    # Emit wave schedule as comment for documentation
    lines.append('/* Wave schedule (static analysis, proven independent):')
    for wi, wave in enumerate(schedule):
        wave_ops = [instrs[i].op for i in wave]
        lines.append(f' * Wave {wi}: {wave_ops}')
    lines.append(' */')
    lines.append('')

    # For the parallel executor with control flow, we implement the
    # wave-parallel inner loop: each loop body executes waves sequentially
    # but within each wave, independent instructions run in parallel.
    #
    # This requires identifying which instructions are in the loop body
    # and emitting them wave by wave with OpenMP.
    #
    # For now: emit a reference executor that uses the ZCE C VM,
    # plus a parallel version that executes each wave with OpenMP
    # for any pure-computation waves (no control flow within the wave).

    lines += [
        '#define ZCE_IMPLEMENTATION',
        '#define ZCE_MAX_SLOTS 512',
        '#include "zce_core.h"',
        '',
        f'int {label}_run(void) {{',
        '    ZCE_VM vm;',
        '    zce_init(&vm);',
        '    zce_install_std(&vm);',
        f'    zce_run(&vm, _prog, sizeof(_prog));',
        '    return (int)vm.slots[0];',
        '}',
        '',
        f'/* Wave-parallel version (for pure-computation subsets of the program) */',
        f'int {label}_parallel_waves(void) {{',
        '    /* Execute data-only instructions wave by wave, parallel within each wave */',
        f'    int32_t snap[MAX_SLOTS];',
        '    memset(slots, 0, sizeof(slots));',
        '',
    ]

    # Emit only data instructions, wave by wave, with OpenMP
    for wi, wave in enumerate(schedule):
        wave_instrs = [instrs[i] for i in wave]
        data_instrs = [i for i in wave_instrs
                       if i.op not in ('JIF','JMP','CALL','RET')]
        if not data_instrs:
            continue

        lines.append(f'    /* Wave {wi}: {len(data_instrs)} data instructions */')
        if parallel and len(data_instrs) > 1:
            lines.append('    memcpy(snap, slots, sizeof(slots));')
            lines.append(f'    #pragma omp parallel for schedule(static)')
            lines.append(f'    for (int _i=0; _i<{len(data_instrs)}; _i++) {{')
            lines.append(f'        switch(_i) {{')
            for idx, instr in enumerate(data_instrs):
                c = _instr_to_c(instr, snap='snap', slots='slots').strip()
                lines.append(f'        case {idx}: {c} break;')
            lines.append('        }')
            lines.append('    }')
        else:
            for instr in data_instrs:
                lines.append(_instr_to_c(instr, snap='slots', slots='slots'))
        lines.append('')

    lines += [
        '    return (int)slots[0];',
        '}',
    ]
    return lines


def _find_simd_ranges(instrs):
    """Find runs of 4+ consecutive same-op instructions on consecutive dst slots."""
    from analysis.zce_analysis import SIMD_WIDTH, SIMD_OPS
    groups = []
    by_op = {}
    for i, instr in enumerate(instrs):
        if instr.op in SIMD_OPS:
            by_op.setdefault(instr.op, []).append((i, instr))
    for op, items in by_op.items():
        items.sort(key=lambda x: min(x[1].writes) if x[1].writes else 0)
        run = []
        for idx, instr in items:
            dst = min(instr.writes) if instr.writes else None
            if dst is None: continue
            if not run:
                run.append((idx, instr))
            elif min(run[-1][1].writes) + 1 == dst:
                run.append((idx, instr))
                if len(run) == SIMD_WIDTH:
                    groups.append(run[:])
                    run = []
            else:
                run = [(idx, instr)]
    return groups


# ── Compile and run generated C ───────────────────────────────────────────────

def compile_and_run(c_src, parallel=True, expected_slot=0,
                    vm_dir=None, timeout=15):
    """
    Write c_src to a temp file, compile, run, return (result, time_ms, ok).
    """
    if vm_dir is None:
        vm_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), 'vm')

    flags = ['-O2']
    if parallel:
        flags.append('-fopenmp')

    with tempfile.TemporaryDirectory() as td:
        src_path = os.path.join(td, 'prog.c')
        bin_path = os.path.join(td, 'prog')
        with open(src_path, 'w') as f:
            f.write(c_src)
        cp = subprocess.run(
            ['gcc'] + flags + ['-I', vm_dir, '-o', bin_path, src_path],
            capture_output=True, text=True)
        if cp.returncode != 0:
            return None, 0.0, False, cp.stderr

        rp = subprocess.run([bin_path], capture_output=True,
                            text=True, timeout=timeout)
        if rp.returncode != 0:
            return None, 0.0, False, rp.stderr

        # Parse output: "label: result=N  time=Xms"
        for line in rp.stdout.splitlines():
            if 'result=' in line and 'time=' in line:
                parts = line.split()
                result_part = [p for p in parts if p.startswith('result=')]
                time_part   = [p for p in parts if p.startswith('time=')]
                if result_part and time_part:
                    result = int(result_part[0].split('=')[1])
                    time_ms = float(time_part[0].replace('time=', '').replace('ms', ''))
                    return result, time_ms, True, ''

        return None, 0.0, False, f'could not parse output: {rp.stdout}'


def benchmark_parallel_vs_sequential(binary, label='bench', vm_dir=None):
    """
    Generate both sequential and parallel C, compile and run both,
    return comparison dict.
    """
    from vm.zce_vm import run_stream

    # Python reference
    import time
    py_times = []
    for _ in range(10):
        t0 = time.perf_counter()
        vm, ok = run_stream(binary)
        py_times.append((time.perf_counter() - t0) * 1000)
    py_ms     = sum(py_times) / len(py_times)
    py_result = vm.slots[0]

    r  = analyze(binary)
    ps = r['par_stats']

    # Sequential C
    c_seq = generate_parallel_c(binary, parallel=False, label=label)
    seq_result, seq_ms, seq_ok, seq_err = compile_and_run(
        c_seq, parallel=False, vm_dir=vm_dir)

    # Parallel C
    c_par = generate_parallel_c(binary, parallel=True, label=label)
    par_result, par_ms, par_ok, par_err = compile_and_run(
        c_par, parallel=True, vm_dir=vm_dir)

    return {
        'label':             label,
        'py_ms':             round(py_ms, 3),
        'py_result':         py_result,
        'seq_ms':            seq_ms,
        'seq_result':        seq_result,
        'seq_ok':            seq_ok,
        'par_ms':            par_ms,
        'par_result':        par_result,
        'par_ok':            par_ok,
        'par_speedup_c':     round(seq_ms / par_ms, 2) if par_ok and par_ms > 0 else None,
        'c_vs_py_speedup':   round(py_ms / seq_ms, 1) if seq_ok and seq_ms > 0 else None,
        'theoretical_speedup': round(ps['theoretical_speedup'], 2),
        'waves':             ps['waves'],
        'instrs':            ps['total_instrs'],
        'tier_safe':         r['tier_safe'],
        'agrees':            (seq_result == py_result and
                              (not par_ok or par_result == py_result)),
        'seq_err':           seq_err,
        'par_err':           par_err,
    }
