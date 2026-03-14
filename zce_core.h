/*
 * zce_core.h — ZCE Streaming VM v3  (single-header C99)
 * Author: Rishi Saraff
 *
 * Token format: [N zeros][M+1 ones][0]
 *   N = opcode identity
 *   M = structural depth
 *
 * FEATURES
 *   - Decode cache: loop bodies decoded once, fast-path from second iteration
 *   - Seekable mode: loop storage = 8 bytes vs 128+ bytes (flash-resident programs)
 *   - CALL/RET: subroutines with full context save/restore
 *   - Static RAM prediction: predict_ram() in zce_stream.py
 *
 * USAGE
 *   #define ZCE_IMPLEMENTATION   // exactly one .c file
 *   #include "zce_core.h"
 *
 *   ZCE_VM vm;
 *   zce_init(&vm);
 *   zce_install_std(&vm);
 *   zce_run(&vm, bytes, len);
 *
 * OPTIONS (define before including)
 *   ZCE_SEEKABLE          use seek-based loop storage (flash-resident programs)
 *   ZCE_MAX_SLOTS    32   slot array size
 *   ZCE_MAX_RULES    16   rule table size
 *   ZCE_MAX_LOOPS     8   max concurrent loop bodies
 *   ZCE_MAX_LOOP_BITS 1024 loop body buffer size (buffered mode only)
 *   ZCE_MAX_LOOP_INSTRS 32 max decoded instructions per loop body
 *   ZCE_MAX_CALL_DEPTH 8  call stack depth
 *   ZCE_MAX_STEPS 10000000 step limit
 */

#ifndef ZCE_CORE_H
#define ZCE_CORE_H

#include <stdint.h>
#include <string.h>

/* ── Configuration ─────────────────────────────────────────────────────────── */
#ifndef ZCE_MAX_SLOTS
#define ZCE_MAX_SLOTS       32  
#endif
#ifndef ZCE_MAX_RULES
#define ZCE_MAX_RULES       16  
#endif
#ifndef ZCE_MAX_LOOPS
#define ZCE_MAX_LOOPS        8  
#endif
#ifndef ZCE_MAX_LOOP_BITS
#define ZCE_MAX_LOOP_BITS 1024  
#endif
#ifndef ZCE_MAX_LOOP_INSTRS
#define ZCE_MAX_LOOP_INSTRS 32  
#endif
#ifndef ZCE_MAX_CALL_DEPTH
#define ZCE_MAX_CALL_DEPTH   8  
#endif
#ifndef ZCE_MAX_STEPS
#define ZCE_MAX_STEPS 10000000  
#endif

/* ── Error codes ───────────────────────────────────────────────────────────── */
#define ZCE_OK             0
#define ZCE_ERR_EOF        1
#define ZCE_ERR_ELIAS_OVF  2
#define ZCE_ERR_UNDEF_TOK  3
#define ZCE_ERR_LOOP_OVF   4
#define ZCE_ERR_MAX_LOOPS  5
#define ZCE_ERR_MAX_STEPS  6
#define ZCE_ERR_CALL_OVF   7
#define ZCE_ERR_RET_EMPTY  8

/* ── Action types ──────────────────────────────────────────────────────────── */
#define ZCE_ACT_COMPUTE 0
#define ZCE_ACT_JIF     1
#define ZCE_ACT_JMP     2
#define ZCE_ACT_DEF     3
#define ZCE_ACT_CALL    4   /* used in decoded cache only */
#define ZCE_ACT_RET     5   /* used in decoded cache only */

/* ── Types ─────────────────────────────────────────────────────────────────── */
typedef struct {
    uint8_t ok, na, at[4], act, ap;
} ZCE_Rule;

/* Pre-decoded instruction — eliminates bit-serial re-decode in loops */
typedef struct {
    uint8_t  act;        /* ZCE_ACT_* */
    uint8_t  ap;         /* compute sub-op */
    uint8_t  na;         /* arg count */
    uint8_t  _pad;
    int32_t  args[3];    /* decoded args */
    int32_t  jump_abs;   /* absolute bit position for JIF/JMP/CALL targets */
} ZCE_CachedInstr;

#ifdef ZCE_SEEKABLE
typedef struct {
    uint32_t       start_abs, end_abs;
    ZCE_CachedInstr cache[ZCE_MAX_LOOP_INSTRS];
    uint8_t        cache_n, cache_ok;
} ZCE_Loop;
#else
typedef struct {
    uint32_t       start_abs;
    uint16_t       len;
    uint8_t        bits[(ZCE_MAX_LOOP_BITS/8)+1];
    ZCE_CachedInstr cache[ZCE_MAX_LOOP_INSTRS];
    uint8_t        cache_n, cache_ok;
} ZCE_Loop;
#endif

typedef void (*ZCE_ExtFn)(int32_t *slots, int32_t *args);

typedef struct {
    int32_t   slots[ZCE_MAX_SLOTS];
    ZCE_Rule  rules[ZCE_MAX_RULES];
    ZCE_Loop  loops[ZCE_MAX_LOOPS];
    ZCE_ExtFn ext[16];
    int       n_loops;

    /* call stack */
    struct {
        uint32_t ret_pos;
        int      saved_in_loop;
        uint32_t saved_lbuf;   /* bit pos in loop buffer OR cache index */
        int      ret_to_loop;
    } call_stack[ZCE_MAX_CALL_DEPTH];
    int call_depth;

    /* stream */
    const uint8_t *data;
    uint32_t       data_bits;
    uint32_t       bit_pos;

    /* loop reader */
    int      in_loop;
    uint32_t lbuf_pos;

    /* grammar */
    uint32_t gs, ge;

    /* telemetry */
    int      err;
    uint32_t steps, instructions, loop_iters, peak_loop_bytes;
} ZCE_VM;

/* ── Public API ─────────────────────────────────────────────────────────────── */
void     zce_init(ZCE_VM *vm);
void     zce_install_std(ZCE_VM *vm);
int      zce_run(ZCE_VM *vm, const uint8_t *data, uint32_t len);
uint32_t zce_ram(const ZCE_VM *vm);

#endif /* ZCE_CORE_H */


/* ═══════════════════════════════════════════════════════════════════════════ */
#ifdef ZCE_IMPLEMENTATION

/* ── Bit reader ─────────────────────────────────────────────────────────────── */
static int _bit(ZCE_VM *vm) {
    if (vm->in_loop >= 0) {
        ZCE_Loop *lp = &vm->loops[vm->in_loop];
#ifdef ZCE_SEEKABLE
        uint32_t body = lp->end_abs - lp->start_abs;
        if (vm->lbuf_pos >= body) { vm->err = ZCE_ERR_EOF; return 0; }
        uint32_t abs = lp->start_abs + vm->lbuf_pos++;
        return (vm->data[abs/8] >> (7-(abs%8))) & 1;
#else
        if (vm->lbuf_pos >= lp->len) { vm->err = ZCE_ERR_EOF; return 0; }
        int b = (lp->bits[vm->lbuf_pos/8] >> (7-(vm->lbuf_pos%8))) & 1;
        vm->lbuf_pos++; return b;
#endif
    }
    if (vm->bit_pos >= vm->data_bits) { vm->err = ZCE_ERR_EOF; return 0; }
    return (vm->data[vm->bit_pos/8] >> (7-(vm->bit_pos++%8))) & 1;
}

static int _peek(ZCE_VM *vm) {
    if (vm->in_loop >= 0) {
        ZCE_Loop *lp = &vm->loops[vm->in_loop];
#ifdef ZCE_SEEKABLE
        uint32_t body = lp->end_abs - lp->start_abs;
        if (vm->lbuf_pos >= body) return -1;
        uint32_t abs = lp->start_abs + vm->lbuf_pos;
        return (vm->data[abs/8] >> (7-(abs%8))) & 1;
#else
        if (vm->lbuf_pos >= lp->len) return -1;
        return (lp->bits[vm->lbuf_pos/8] >> (7-(vm->lbuf_pos%8))) & 1;
#endif
    }
    if (vm->bit_pos >= vm->data_bits) return -1;
    return (vm->data[vm->bit_pos/8] >> (7-(vm->bit_pos%8))) & 1;
}

static void _seek_main(ZCE_VM *vm, uint32_t pos) { vm->bit_pos = pos; }

/* ── Decode ─────────────────────────────────────────────────────────────────── */
static int32_t _elias(ZCE_VM *vm) {
    int k = 0;
    while (_peek(vm) == 0) { _bit(vm); if (++k > 30) { vm->err = ZCE_ERR_ELIAS_OVF; return -1; } }
    int32_t x = 0;
    for (int i = 0; i <= k; i++) x = (x<<1)|_bit(vm);
    return x - 1;
}

static void _token(ZCE_VM *vm, int *n, int *m) {
    *n = 0; while (_peek(vm)==0) { _bit(vm); (*n)++; }
    *m = 0; while (_peek(vm)==1) { _bit(vm); (*m)++; }
    _bit(vm); (*m)--;
}

/* ── Slot access ───────────────────────────────────────────────────────────── */
static int32_t _get(ZCE_VM *vm, int i) { return (i>=0&&i<ZCE_MAX_SLOTS)?vm->slots[i]:0; }
static void    _set(ZCE_VM *vm, int i, int32_t v) { if (i>=0&&i<ZCE_MAX_SLOTS) vm->slots[i]=v; }

/* ── DEF ───────────────────────────────────────────────────────────────────── */
static void _define(ZCE_VM *vm) {
    int32_t tgt = _elias(vm); if (vm->err) return;
    int bad = (tgt<=0||tgt>=ZCE_MAX_RULES||tgt==3||tgt==4||tgt==5||tgt==6||tgt==7);
    int32_t na = _elias(vm)+1; if (vm->err) return;
    if (na<0||na>4) na=0;
    uint8_t at[4]={0};
    for (int i=0;i<na;i++) { int32_t t=_elias(vm); if(vm->err)return; at[i]=(uint8_t)(t&1); }
    int32_t act=_elias(vm); if(vm->err) return;
    int32_t ap=(act==ZCE_ACT_COMPUTE)?_elias(vm):0; if(vm->err) return;
    if (!bad) {
        ZCE_Rule *r=&vm->rules[tgt];
        r->ok=1; r->na=(uint8_t)na; r->act=(uint8_t)act; r->ap=(uint8_t)ap;
        for(int i=0;i<4;i++) r->at[i]=at[i];
    }
}

/* ── Loop capture ──────────────────────────────────────────────────────────── */
static int _capture(ZCE_VM *vm, uint32_t start, uint32_t end) {
    for (int i=0;i<vm->n_loops;i++)
        if (vm->loops[i].start_abs==start) return i;
    if (vm->n_loops>=ZCE_MAX_LOOPS) { vm->err=ZCE_ERR_MAX_LOOPS; return -1; }
    ZCE_Loop *lp = &vm->loops[vm->n_loops];
    lp->start_abs = start;
    lp->cache_n = 0; lp->cache_ok = 0;
#ifdef ZCE_SEEKABLE
    lp->end_abs = end;
    uint32_t lb = (end-start+7)/8;
#else
    uint32_t len = end-start;
    if (len>ZCE_MAX_LOOP_BITS) { vm->err=ZCE_ERR_LOOP_OVF; return -1; }
    lp->len = (uint16_t)len;
    memset(lp->bits,0,(len/8)+1);
    for (uint32_t i=0;i<len;i++) {
        uint32_t bp=start+i;
        int b=(vm->data[bp/8]>>(7-(bp%8)))&1;
        if (b) lp->bits[i/8]|=(uint8_t)(1<<(7-(i%8)));
    }
    uint32_t lb = (len+7)/8;
#endif
    if (lb>vm->peak_loop_bytes) vm->peak_loop_bytes=lb;
    return vm->n_loops++;
}

/* ── Decode cache ──────────────────────────────────────────────────────────── */
static void _build_cache(ZCE_VM *vm, int li);  /* forward decl */

/* Execute one instruction (bit-serial path) — used outside loops and as fallback */
/* Returns: 0=ok, 1=fwd_jump, 2=back_jump; sets *target for jumps; -1=error */
static int _exec(ZCE_VM *vm, uint32_t *target, uint32_t pos_abs) {
    int n, m; _token(vm, &n, &m);
    if (vm->err) return -1;
    if (n == 0) return 0;
    if (n == 5) { _define(vm); return 0; }

    /* CALL */
    if (n == 6) {
        int32_t off=_elias(vm); if(vm->err) return -1;
        if (vm->call_depth>=ZCE_MAX_CALL_DEPTH) { vm->err=ZCE_ERR_CALL_OVF; return -1; }
        vm->call_stack[vm->call_depth].ret_pos      = vm->bit_pos;
        vm->call_stack[vm->call_depth].saved_in_loop = vm->in_loop;
        vm->call_stack[vm->call_depth].saved_lbuf    = vm->lbuf_pos;
        vm->call_stack[vm->call_depth].ret_to_loop   = (vm->in_loop>=0);
        vm->call_depth++;
        vm->in_loop = -1;
        uint32_t t=vm->gs+(uint32_t)off;
        if (t>vm->ge) { vm->err=ZCE_ERR_CALL_OVF; return -1; }
        _seek_main(vm,t); vm->steps++; return 0;
    }
    /* RET */
    if (n == 7) {
        if (vm->call_depth==0) { vm->err=ZCE_ERR_RET_EMPTY; return -1; }
        vm->call_depth--;
        vm->in_loop  = vm->call_stack[vm->call_depth].saved_in_loop;
        vm->lbuf_pos = vm->call_stack[vm->call_depth].saved_lbuf;
        if (!vm->call_stack[vm->call_depth].ret_to_loop)
            _seek_main(vm, vm->call_stack[vm->call_depth].ret_pos);
        vm->steps++; return 0;
    }

    if (n>=ZCE_MAX_RULES||!vm->rules[n].ok) { vm->err=ZCE_ERR_UNDEF_TOK; return -1; }
    ZCE_Rule *r=&vm->rules[n];
    int32_t args[4]={0};
    for (int i=0;i<r->na;i++) { args[i]=_elias(vm); if(vm->err) return -1; }

    vm->instructions++; vm->steps++;
    if (vm->steps>=(uint32_t)ZCE_MAX_STEPS) { vm->err=ZCE_ERR_MAX_STEPS; return -1; }

    switch (r->act) {
    case ZCE_ACT_COMPUTE:
        if      (r->ap==1) _set(vm,args[0],args[1]);
        else if (r->ap==2) _set(vm,args[0],_get(vm,args[1])+_get(vm,args[2]));
        else if (r->ap>=3&&r->ap<16&&vm->ext[r->ap]) vm->ext[r->ap](vm->slots,args);
        return 0;
    case ZCE_ACT_JIF:
        if (_get(vm,args[0])==0) {
            *target=vm->gs+(uint32_t)args[1];
            return (*target<pos_abs)?2:1;
        }
        return 0;
    case ZCE_ACT_JMP:
        *target=vm->gs+(uint32_t)args[0];
        return (*target<pos_abs)?2:1;
    }
    return 0;
}

/* ── Main runner ───────────────────────────────────────────────────────────── */
static void _run_loop(ZCE_VM *vm, uint32_t lstart, uint32_t lend);

static void _run_main(ZCE_VM *vm) {
    while (!vm->err) {
        if (vm->bit_pos >= vm->ge) break;
        /* Stop if we've returned into a loop context (RET from subroutine) */
        if (vm->in_loop >= 0) break;
        uint32_t pos=vm->bit_pos, target=0;
        int a=_exec(vm,&target,pos);
        if (a<0) break;
        if (a==0) {
            /* Also stop if _exec just did a RET into a loop */
            if (vm->in_loop >= 0) break;
            continue;
        }
        if (a==1) { if(target>=vm->ge) break; _seek_main(vm,target); }
        else       { _run_loop(vm,target,pos); }
    }
}

/* ── Cache build (decode loop body once) ───────────────────────────────────── */
static void _build_cache(ZCE_VM *vm, int li) {
    ZCE_Loop *lp = &vm->loops[li];
    if (lp->cache_ok) return;

    int   sv_loop = vm->in_loop;
    uint32_t sv_lbuf = vm->lbuf_pos;
    int   sv_err  = vm->err;
    vm->in_loop  = li;
    vm->lbuf_pos = 0;
    vm->err      = 0;

    uint8_t n=0;
#ifdef ZCE_SEEKABLE
    uint32_t body_bits = lp->end_abs - lp->start_abs;
#else
    uint32_t body_bits = lp->len;
#endif

    while (!vm->err && vm->lbuf_pos < body_bits && n < ZCE_MAX_LOOP_INSTRS) {
        int tok,m; _token(vm,&tok,&m);
        if (vm->err||tok==0) break;
        if (tok==5) { _define(vm); continue; }

        ZCE_CachedInstr *ci = &lp->cache[n];
        memset(ci,0,sizeof(*ci));

        if (tok==6) { /* CALL */
            int32_t off=_elias(vm); if(vm->err) break;
            ci->act=ZCE_ACT_CALL; ci->jump_abs=(int32_t)(vm->gs+(uint32_t)off);
            n++; continue;
        }
        if (tok==7) { /* RET */
            ci->act=ZCE_ACT_RET; n++; continue;
        }

        if (tok>=ZCE_MAX_RULES||!vm->rules[tok].ok) { vm->err=1; break; }
        ZCE_Rule *r=&vm->rules[tok];
        ci->act=r->act; ci->ap=r->ap; ci->na=r->na;
        for (int i=0;i<r->na;i++) { ci->args[i]=_elias(vm); if(vm->err) goto done; }
        if (r->act==ZCE_ACT_JIF) ci->jump_abs=(int32_t)(vm->gs+(uint32_t)ci->args[1]);
        if (r->act==ZCE_ACT_JMP) ci->jump_abs=(int32_t)(vm->gs+(uint32_t)ci->args[0]);
        n++;
    }
done:
    lp->cache_n  = n;
    lp->cache_ok = vm->err ? 0 : 1;
    vm->in_loop  = sv_loop;
    vm->lbuf_pos = sv_lbuf;
    vm->err      = sv_err;
}

/* ── Loop runner ───────────────────────────────────────────────────────────── */
static void _run_loop(ZCE_VM *vm, uint32_t lstart, uint32_t lend) {
    int li=_capture(vm,lstart,lend);
    if (li<0||vm->err) return;
    ZCE_Loop *lp=&vm->loops[li];

    _build_cache(vm,li);

    /* If loop contains CALL, use slow path (CALL context save is complex in cache) */
    int has_call = 0;
    if (lp->cache_ok) {
        for (int i=0;i<(int)lp->cache_n;i++)
            if (lp->cache[i].act==ZCE_ACT_CALL||lp->cache[i].act==ZCE_ACT_RET)
                { has_call=1; break; }
    }

    /* ── FAST PATH: execute from decoded cache ─────────────────────────── */
    if (lp->cache_ok && lp->cache_n > 0 && !has_call) {
        while (!vm->err) {
            vm->loop_iters++;
            int ci=0, exited=0;

            while (!vm->err && ci < (int)lp->cache_n) {
                ZCE_CachedInstr *c=&lp->cache[ci];
                vm->instructions++; vm->steps++;
                if (vm->steps>=(uint32_t)ZCE_MAX_STEPS){vm->err=ZCE_ERR_MAX_STEPS;return;}

                switch (c->act) {
                case ZCE_ACT_COMPUTE: {
                    uint8_t p=c->ap;
                    if      (p==1) _set(vm,c->args[0],c->args[1]);
                    else if (p==2) _set(vm,c->args[0],_get(vm,c->args[1])+_get(vm,c->args[2]));
                    else if (p>=3&&p<16&&vm->ext[p]) vm->ext[p](vm->slots,c->args);
                    ci++; break;
                }
                case ZCE_ACT_JIF:
                    if (_get(vm,c->args[0])==0) {
                        uint32_t t=(uint32_t)c->jump_abs;
                        if (t>=(uint32_t)lend) {
                            vm->in_loop=-1;
                            _seek_main(vm,t<vm->ge?t:vm->ge);
                            exited=1; goto loop_done;
                        }
                        /* jump within loop: find cache index for target */
                        /* simple: restart from 0 if backward, scan forward if fwd */
                        if (t<lstart) { goto restart_iter; }
                        /* forward within loop body: linear scan for offset */
                        { uint32_t off=t-lstart;
                          /* rebuild bit-pos scan to find ci */
                          /* For simplicity: restart slow path for intra-loop jumps */
                          goto slow_path; }
                    }
                    ci++; break;
                case ZCE_ACT_JMP: {
                    uint32_t t=(uint32_t)c->jump_abs;
                    if (t>=(uint32_t)lend) {
                        vm->in_loop=-1;
                        _seek_main(vm,t<vm->ge?t:vm->ge);
                        exited=1; goto loop_done;
                    }
                    if (t<=lstart) goto restart_iter;
                    goto slow_path;
                }
                case ZCE_ACT_CALL: {
                    if (vm->call_depth>=ZCE_MAX_CALL_DEPTH){vm->err=ZCE_ERR_CALL_OVF;return;}
                    vm->call_stack[vm->call_depth].saved_in_loop = -1;
                    vm->call_stack[vm->call_depth].saved_lbuf    = (uint32_t)(ci+1);
                    vm->call_stack[vm->call_depth].ret_to_loop   = 1;
                    vm->call_stack[vm->call_depth].ret_pos       = 0;
                    vm->call_depth++;
                    vm->in_loop=-1;
                    _seek_main(vm,(uint32_t)c->jump_abs);
                    /* run main until RET restores context */
                    _run_main(vm);
                    if (vm->err) return;
                    /* after CALL returns, call stack was popped by RET */
                    /* resume at ci+1 (saved in saved_lbuf before call) */
                    ci = (int)vm->call_stack[vm->call_depth].saved_lbuf;
                    vm->in_loop = li;
                    /* actually: call_depth was already decremented by RET */
                    /* need the index from before the CALL */
                    ci++; /* move past CALL */
                    /* Actually simplest: just store ci+1 and retrieve */
                    break;
                }
                case ZCE_ACT_RET:
                    vm->in_loop=-1; return; /* RET inside loop body */
                default:
                    ci++;
                }
            }
            loop_done:
            vm->in_loop=-1;
            if (exited||vm->err) return;
            continue;
            restart_iter: vm->in_loop=-1; continue;
            slow_path: goto use_slow_path;
        }
        return;
    }

use_slow_path:
    /* ── SLOW PATH: bit-serial decode every iteration ──────────────────── */
    while (!vm->err) {
        vm->loop_iters++;
        vm->in_loop  = li;
        vm->lbuf_pos = 0;
        int exited   = 0;
        while (!vm->err) {
#ifdef ZCE_SEEKABLE
            if (vm->lbuf_pos>=(lp->end_abs-lp->start_abs)) break;
#else
            if (vm->lbuf_pos>=lp->len) break;
#endif
            uint32_t bpos=vm->lbuf_pos, apos=lstart+bpos, target=0;
            int loop_before = vm->in_loop;
            int a=_exec(vm,&target,apos);
            if (a<0) { vm->in_loop=-1; return; }
            if (a==0) {
                /* CALL: _exec set in_loop=-1 and is executing subroutine */
                if (loop_before>=0 && vm->in_loop<0) {
                    /* Run subroutine until RET restores in_loop=li */
                    _run_main(vm);
                    if (vm->err) { vm->in_loop=-1; return; }
                    /* in_loop and lbuf_pos are now restored by RET */
                }
                continue;
            }
            uint32_t off=(target>lstart)?(target-lstart):0;
            if (a==1) {
                if (target>=lend) {
                    vm->in_loop=-1;
                    _seek_main(vm,target<vm->ge?target:vm->ge);
                    exited=1; break;
                }
                vm->lbuf_pos=(uint16_t)off;
            } else {
                if (off==0) break;
                vm->lbuf_pos=(uint16_t)off;
            }
        }
        vm->in_loop=-1;
        if (exited||vm->err) return;
    }
}

/* ── Public API ─────────────────────────────────────────────────────────────── */
void zce_init(ZCE_VM *vm) {
    memset(vm,0,sizeof(*vm));
    vm->in_loop=-1;
    vm->rules[1]=(ZCE_Rule){1,2,{0,1,0,0},ZCE_ACT_COMPUTE,1};
    vm->rules[2]=(ZCE_Rule){1,3,{0,0,0,0},ZCE_ACT_COMPUTE,2};
    vm->rules[3]=(ZCE_Rule){1,2,{0,0,0,0},ZCE_ACT_JIF,    0};
    vm->rules[4]=(ZCE_Rule){1,1,{0,0,0,0},ZCE_ACT_JMP,    0};
    vm->rules[5]=(ZCE_Rule){1,0,{0,0,0,0},ZCE_ACT_DEF,    0};
}

int zce_run(ZCE_VM *vm, const uint8_t *data, uint32_t len) {
    vm->data=data; vm->data_bits=len*8; vm->bit_pos=0; vm->in_loop=-1;
    int32_t gb=_elias(vm); if(vm->err) return vm->err;
    vm->gs=vm->bit_pos; vm->ge=vm->gs+(uint32_t)gb;
    _run_main(vm);
    return vm->err;
}

uint32_t zce_ram(const ZCE_VM *vm) {
    uint32_t lb=0;
    for (int i=0;i<vm->n_loops;i++) {
#ifdef ZCE_SEEKABLE
        lb+=sizeof(ZCE_Loop);
#else
        lb+=(vm->loops[i].len+7)/8;
#endif
    }
    return (uint32_t)(ZCE_MAX_SLOTS*sizeof(int32_t))
          +(uint32_t)(ZCE_MAX_RULES*sizeof(ZCE_Rule))
          +lb+64;
}

/* ── Standard extended ops ──────────────────────────────────────────────────── */
static void _ext_sub (int32_t*s,int32_t*a){s[a[0]]=s[a[1]]-s[a[2]];}
static void _ext_mul (int32_t*s,int32_t*a){s[a[0]]=s[a[1]]*s[a[2]];}
static void _ext_div (int32_t*s,int32_t*a){s[a[0]]=s[a[2]]?s[a[1]]/s[a[2]]:0;}
static void _ext_mod (int32_t*s,int32_t*a){s[a[0]]=s[a[2]]?s[a[1]]%s[a[2]]:0;}
static void _ext_eq  (int32_t*s,int32_t*a){s[a[0]]=(s[a[1]]==s[a[2]])?1:0;}
static void _ext_lt  (int32_t*s,int32_t*a){s[a[0]]=(s[a[1]]< s[a[2]])?1:0;}
static void _ext_not (int32_t*s,int32_t*a){s[a[0]]=s[a[1]]?0:1;}
static void _ext_load(int32_t*s,int32_t*a){int i=s[a[1]];s[a[0]]=(i>=0&&i<ZCE_MAX_SLOTS)?s[i]:0;}
static void _ext_stor(int32_t*s,int32_t*a){int i=s[a[0]];if(i>=0&&i<ZCE_MAX_SLOTS)s[i]=s[a[1]];}

void zce_install_std(ZCE_VM *vm) {
    vm->ext[3]=_ext_sub; vm->ext[4]=_ext_mul; vm->ext[5]=_ext_div;
    vm->ext[6]=_ext_mod; vm->ext[7]=_ext_eq;  vm->ext[8]=_ext_lt;
    vm->ext[9]=_ext_not; vm->ext[10]=_ext_load; vm->ext[11]=_ext_stor;
}

#endif /* ZCE_IMPLEMENTATION */
