/*
 * zce_core.h  —  ZCE Virtual Machine v2  (single-header C99)
 * Author: Rishi Saraff
 *
 * Token format:  [N zeros][M+1 ones][0 terminator]
 *   N = opcode identity (0 = NOP)
 *   M = structural depth (0 = base, 1 = extended, 2 = nested)
 *
 * Self-delimiting header: every program begins with elias(body_bits).
 * The VM reads this before any instruction and knows where the program ends.
 * Programs can be concatenated without boundary markers.
 *
 * Base instructions
 *   tok 1  SET   slots[d] = literal
 *   tok 2  ADD   slots[d] = slots[a] + slots[b]
 *   tok 3  JIF   if slots[cond]==0: ip = gs + elias_offset
 *   tok 4  JMP   ip = gs + elias_offset
 *   tok 5  DEF   define extended operation
 *   tok 6  CALL  push return address, jump to subroutine
 *   tok 7  RET   pop return address, continue
 *
 * Extended operations (defined via DEF, tokens 8-16)
 *   SUB MUL DIV MOD EQ LT NOT LOAD STOR
 *
 * Build modes
 *   Default (buffered):  loop bodies buffered in RAM.  ~1664B footprint.
 *   ZCE_SEEKABLE:        loop bodies reference flash positions. ~640B footprint.
 *   ZCE_TIGHT:           seekable + minimal slot array.         ~336B footprint.
 *
 * Usage
 *   #define ZCE_IMPLEMENTATION   // exactly one translation unit
 *   #include "zce_core.h"
 *
 *   ZCE_VM vm;
 *   zce_init(&vm);
 *   zce_install_std(&vm);
 *   int ok = zce_run(&vm, program_bytes, program_len);
 *   int result = vm.slots[0];
 *
 * Compile-time knobs (define before including)
 *   ZCE_MAX_SLOTS       32    slot array size
 *   ZCE_MAX_RULES       20    rule table entries
 *   ZCE_MAX_LOOPS        8    concurrent buffered loop bodies
 *   ZCE_MAX_LOOP_BITS 1024    bits per buffered loop body
 *   ZCE_MAX_CALL_DEPTH   8    CALL/RET stack depth
 *   ZCE_MAX_STEPS  1000000    step limit
 */

#ifndef ZCE_CORE_H
#define ZCE_CORE_H

#include <stdint.h>
#include <string.h>

/* ── Compile-time knobs ──────────────────────────────────────────────────── */
#ifndef ZCE_MAX_SLOTS
#  define ZCE_MAX_SLOTS      32
#endif
#ifndef ZCE_MAX_RULES
#  define ZCE_MAX_RULES      20
#endif
#ifndef ZCE_MAX_LOOPS
#  define ZCE_MAX_LOOPS       8
#endif
#ifndef ZCE_MAX_LOOP_BITS
#  define ZCE_MAX_LOOP_BITS 1024
#endif
#ifndef ZCE_MAX_CALL_DEPTH
#  define ZCE_MAX_CALL_DEPTH  8
#endif
#ifndef ZCE_MAX_STEPS
#  define ZCE_MAX_STEPS  1000000
#endif

/* ── Error codes ─────────────────────────────────────────────────────────── */
#define ZCE_OK              0
#define ZCE_ERR_EOF         1
#define ZCE_ERR_ELIAS_OVF   2
#define ZCE_ERR_UNDEF_TOK   3
#define ZCE_ERR_JIF_OOB     4
#define ZCE_ERR_JMP_OOB     5
#define ZCE_ERR_CALL_OOB    6
#define ZCE_ERR_CALL_STK    7
#define ZCE_ERR_RET_EMPTY   8
#define ZCE_ERR_STEPS       9
#define ZCE_ERR_LOOP_OVF   10
#define ZCE_ERR_LOOP_BIG   11

/* ── Argument type codes ─────────────────────────────────────────────────── */
#define ZCE_AT_SLOT   0
#define ZCE_AT_VALUE  1
#define ZCE_AT_RAW32  2

/* ── Rule table entry ────────────────────────────────────────────────────── */
typedef struct {
    uint8_t  ok;
    uint8_t  na;
    uint8_t  at[4];
    uint8_t  act;
    uint8_t  ap;
} ZCE_Rule;

/* ── Buffered loop body ───────────────────────────────────────────────────── */
#ifndef ZCE_SEEKABLE
typedef struct {
    uint32_t start_abs;
    uint16_t nbits;
    uint8_t  bits[ZCE_MAX_LOOP_BITS / 8];
} ZCE_LoopBuf;
#endif

/* ── VM state ────────────────────────────────────────────────────────────── */
typedef struct ZCE_VM {
    int32_t  slots[ZCE_MAX_SLOTS];
    ZCE_Rule rules[ZCE_MAX_RULES];
    uint32_t call_stack[ZCE_MAX_CALL_DEPTH];
    uint8_t  call_depth;
    void   (*ext[20])(int32_t *slots, int32_t *args);
    uint32_t steps;
    uint8_t  err;
#ifdef ZCE_SEEKABLE
    const uint8_t *_data;
    uint32_t       _data_len;
#else
    ZCE_LoopBuf loops[ZCE_MAX_LOOPS];
    uint8_t     nloops;
#endif
} ZCE_VM;

/* ── Public API ──────────────────────────────────────────────────────────── */
void        zce_init       (ZCE_VM *vm);
void        zce_install_std(ZCE_VM *vm);
int         zce_run        (ZCE_VM *vm, const uint8_t *data, uint32_t len);
const char *zce_err_str    (uint8_t err);

/* ══════════════════════════════════════════════════════════════════════════
 * Implementation
 * ══════════════════════════════════════════════════════════════════════════ */
#ifdef ZCE_IMPLEMENTATION

/* ── Bit-stream reader ───────────────────────────────────────────────────── */
typedef struct { const uint8_t *data; uint32_t len; uint32_t pos; } ZCE_BS;

static void     bs_init(ZCE_BS *b, const uint8_t *d, uint32_t l) { b->data=d; b->len=l; b->pos=0; }
static void     bs_seek(ZCE_BS *b, uint32_t p)  { b->pos = p; }
static uint32_t bs_tell(const ZCE_BS *b)         { return b->pos; }

static int bs_read(ZCE_BS *b) {
    if ((b->pos >> 3) >= b->len) return -1;
    int bit = (b->data[b->pos >> 3] >> (7 - (b->pos & 7))) & 1;
    b->pos++; return bit;
}
static int bs_peek(const ZCE_BS *b) {
    if ((b->pos >> 3) >= b->len) return -1;
    return (b->data[b->pos >> 3] >> (7 - (b->pos & 7))) & 1;
}

static int32_t bs_elias(ZCE_BS *b, uint8_t *err) {
    int k = 0;
    while (bs_peek(b) == 0) { bs_read(b); if (++k > 30) { *err = ZCE_ERR_ELIAS_OVF; return -1; } }
    uint32_t x = 0;
    for (int i = 0; i <= k; i++) {
        int bit = bs_read(b);
        if (bit < 0) { *err = ZCE_ERR_EOF; return -1; }
        x = (x << 1) | (uint32_t)bit;
    }
    return (int32_t)(x - 1);
}

static int bs_token(ZCE_BS *b, int *depth) {
    int n = 0; while (bs_peek(b) == 0) { bs_read(b); n++; }
    int m = 0; while (bs_peek(b) == 1) { bs_read(b); m++; }
    if (bs_read(b) < 0) return -1;
    *depth = m - 1; return n;
}

static int32_t bs_arg(ZCE_BS *b, uint8_t at, uint8_t *err) {
    if (at == ZCE_AT_RAW32) {
        int32_t v = 0;
        for (int i = 0; i < 32; i++) {
            int bit = bs_read(b);
            if (bit < 0) { *err = ZCE_ERR_EOF; return 0; }
            v = (v << 1) | bit;
        }
        return v;
    }
    return bs_elias(b, err);
}

/* ── Extended op callbacks ───────────────────────────────────────────────── */
static void _ext_sub (int32_t *s, int32_t *a) { s[a[0]] = s[a[1]] - s[a[2]]; }
static void _ext_mul (int32_t *s, int32_t *a) { s[a[0]] = s[a[1]] * s[a[2]]; }
static void _ext_div (int32_t *s, int32_t *a) { s[a[0]] = s[a[2]] ? s[a[1]]/s[a[2]] : 0; }
static void _ext_mod (int32_t *s, int32_t *a) { s[a[0]] = s[a[2]] ? s[a[1]]%s[a[2]] : 0; }
static void _ext_eq  (int32_t *s, int32_t *a) { s[a[0]] = s[a[1]] == s[a[2]] ? 1 : 0; }
static void _ext_lt  (int32_t *s, int32_t *a) { s[a[0]] = s[a[1]] <  s[a[2]] ? 1 : 0; }
static void _ext_not (int32_t *s, int32_t *a) { s[a[0]] = s[a[1]] ? 0 : 1; }
static void _ext_load(int32_t *s, int32_t *a) { int32_t i=s[a[1]]; s[a[0]]=(i>=0&&i<ZCE_MAX_SLOTS)?s[i]:0; }
static void _ext_stor(int32_t *s, int32_t *a) { int32_t i=s[a[0]]; if(i>=0&&i<ZCE_MAX_SLOTS) s[i]=s[a[1]]; }

/* ── DEF handler ─────────────────────────────────────────────────────────── */
static void _do_def(ZCE_VM *vm, ZCE_BS *bs) {
    int32_t tgt = bs_elias(bs, &vm->err); if (vm->err) return;
    int32_t na  = bs_elias(bs, &vm->err) + 1; if (vm->err) return;
    if (na > 4) na = 4;
    uint8_t at[4] = {0,0,0,0};
    for (int i = 0; i < na; i++) { int32_t v = bs_elias(bs, &vm->err); if (vm->err) return; at[i]=(uint8_t)v; }
    int32_t act = bs_elias(bs, &vm->err); if (vm->err) return;
    int32_t ap  = 0;
    if (act == 0) { ap = bs_elias(bs, &vm->err); if (vm->err) return; }
    if (tgt <= 0 || tgt >= ZCE_MAX_RULES || tgt <= 7) return;
    ZCE_Rule *r = &vm->rules[tgt];
    r->ok=1; r->na=(uint8_t)na; r->act=(uint8_t)act; r->ap=(uint8_t)ap;
    for (int i=0;i<4;i++) r->at[i]=at[i];
}

/* ── Slot helpers ────────────────────────────────────────────────────────── */
#define S_GET(vm,i) ((i)>=0&&(i)<ZCE_MAX_SLOTS?(vm)->slots[(i)]:0)
#define S_SET(vm,i,v) do{ if((i)>=0&&(i)<ZCE_MAX_SLOTS)(vm)->slots[(i)]=(v); }while(0)

/* ══════════════════════════════════════════════════════════════════════════
 * Seekable mode execution
 * ══════════════════════════════════════════════════════════════════════════ */
#ifdef ZCE_SEEKABLE

static void _run_range(ZCE_VM *vm, ZCE_BS *bs, uint32_t gs, uint32_t ge) {
    while (!vm->err && vm->steps < (uint32_t)ZCE_MAX_STEPS && bs_tell(bs) < ge) {
        int depth, tok = bs_token(bs, &depth);
        if (tok < 0 || bs_tell(bs) > ge) break;
        if (tok == 0) continue;
        if (tok == 5) { _do_def(vm, bs); continue; }
        if (tok == 6) {
            int32_t off = bs_elias(bs, &vm->err); if (vm->err) break;
            if (vm->call_depth >= ZCE_MAX_CALL_DEPTH) { vm->err=ZCE_ERR_CALL_STK; break; }
            uint32_t t = gs + (uint32_t)off;
            if (t > ge) { vm->err=ZCE_ERR_CALL_OOB; break; }
            vm->call_stack[vm->call_depth++] = bs_tell(bs);
            bs_seek(bs, t); vm->steps++; continue;
        }
        if (tok == 7) {
            if (!vm->call_depth) { vm->err=ZCE_ERR_RET_EMPTY; break; }
            bs_seek(bs, vm->call_stack[--vm->call_depth]); vm->steps++; continue;
        }
        if (tok >= ZCE_MAX_RULES || !vm->rules[tok].ok) { vm->err=ZCE_ERR_UNDEF_TOK; break; }
        ZCE_Rule *r = &vm->rules[tok];
        int32_t args[4]={0,0,0,0};
        for (int i=0;i<r->na;i++) { args[i]=bs_arg(bs,r->at[i],&vm->err); if(vm->err) return; }
        vm->steps++;
        switch (r->act) {
        case 0:
            if      (r->ap==1) S_SET(vm,args[0],args[1]);
            else if (r->ap==2) S_SET(vm,args[0],S_GET(vm,args[1])+S_GET(vm,args[2]));
            else if (r->ap>=3&&r->ap<20&&vm->ext[r->ap]) vm->ext[r->ap](vm->slots,args);
            break;
        case 1: if (!S_GET(vm,args[0])) { uint32_t t=gs+(uint32_t)args[1]; if(t>ge){vm->err=ZCE_ERR_JIF_OOB;return;} bs_seek(bs,t); } break;
        case 2: { uint32_t t=gs+(uint32_t)args[0]; if(t>ge){vm->err=ZCE_ERR_JMP_OOB;return;} bs_seek(bs,t); break; }
        }
    }
}

#else /* buffered mode */

static ZCE_LoopBuf *_find_loop(ZCE_VM *vm, uint32_t start) {
    for (int i=0;i<vm->nloops;i++) if(vm->loops[i].start_abs==start) return &vm->loops[i];
    return NULL;
}

static ZCE_LoopBuf *_capture_loop(ZCE_VM *vm, ZCE_BS *src, uint32_t start, uint32_t end) {
    if (vm->nloops >= ZCE_MAX_LOOPS) { vm->err=ZCE_ERR_LOOP_OVF; return NULL; }
    uint32_t nbits = end - start;
    if (nbits > ZCE_MAX_LOOP_BITS) { vm->err=ZCE_ERR_LOOP_BIG; return NULL; }
    ZCE_LoopBuf *lb = &vm->loops[vm->nloops++];
    lb->start_abs = start; lb->nbits = (uint16_t)nbits;
    memset(lb->bits, 0, sizeof(lb->bits));
    uint32_t saved = bs_tell(src);
    bs_seek(src, start);
    for (uint32_t i=0; i<nbits; i++) {
        int b = bs_read(src);
        if (b < 0) { vm->err=ZCE_ERR_EOF; bs_seek(src,saved); return NULL; }
        if (b) lb->bits[i>>3] |= (uint8_t)(1 << (7-(i&7)));
    }
    bs_seek(src, saved);
    return lb;
}

static int _exec_buf(ZCE_VM *vm, ZCE_LoopBuf *lb, uint32_t gs, uint32_t ge, uint32_t *exit_target) {
    ZCE_BS buf;
    buf.data     = lb->bits;
    buf.len      = (lb->nbits+7)/8;
    buf.pos      = 0;
    uint32_t bas = lb->start_abs;
    uint32_t bae = lb->start_abs + lb->nbits;

    while (!vm->err && vm->steps < (uint32_t)ZCE_MAX_STEPS) {
        uint32_t bp = bs_tell(&buf);
        if (bp >= lb->nbits) { *exit_target = bas; return 0; }
        int depth, tok = bs_token(&buf, &depth);
        if (tok < 0 || bs_tell(&buf) > lb->nbits) { *exit_target = bas; return 0; }
        if (tok == 0) continue;
        if (tok == 5) continue; /* skip DEF in buffer */
        if (tok == 6) { vm->err=ZCE_ERR_CALL_OOB; return 0; }
        if (tok == 7) {
            if (!vm->call_depth) { vm->err=ZCE_ERR_RET_EMPTY; return 0; }
            *exit_target = vm->call_stack[--vm->call_depth]; return 1;
        }
        if (tok >= ZCE_MAX_RULES || !vm->rules[tok].ok) { vm->err=ZCE_ERR_UNDEF_TOK; return 0; }
        ZCE_Rule *r = &vm->rules[tok];
        int32_t args[4]={0,0,0,0};
        for (int i=0;i<r->na;i++) { args[i]=bs_arg(&buf,r->at[i],&vm->err); if(vm->err) return 0; }
        vm->steps++;
        switch (r->act) {
        case 0:
            if      (r->ap==1) S_SET(vm,args[0],args[1]);
            else if (r->ap==2) S_SET(vm,args[0],S_GET(vm,args[1])+S_GET(vm,args[2]));
            else if (r->ap>=3&&r->ap<20&&vm->ext[r->ap]) vm->ext[r->ap](vm->slots,args);
            break;
        case 1: {
            if (!S_GET(vm,args[0])) {
                uint32_t at = gs+(uint32_t)args[1];
                if (at >= bae) { *exit_target=at; if(at>ge){vm->err=ZCE_ERR_JIF_OOB;return 1;} return 1; }
                buf.pos = (at >= bas) ? at-bas : 0;
            }
            break;
        }
        case 2: {
            uint32_t at = gs+(uint32_t)args[0];
            if (at >= bae || at <= bas) { buf.pos=0; }
            else buf.pos = at-bas;
            break;
        }
        }
    }
    *exit_target = bae; return 1;
}

static void _run_range(ZCE_VM *vm, ZCE_BS *bs, uint32_t gs, uint32_t ge) {
    while (!vm->err && vm->steps < (uint32_t)ZCE_MAX_STEPS && bs_tell(bs) < ge) {
        uint32_t ipos = bs_tell(bs);
        int depth, tok = bs_token(bs, &depth);
        if (tok < 0 || bs_tell(bs) > ge) break;
        if (tok == 0) continue;
        if (tok == 5) { _do_def(vm, bs); continue; }
        if (tok == 6) {
            int32_t off=bs_elias(bs,&vm->err); if(vm->err) break;
            if (vm->call_depth>=ZCE_MAX_CALL_DEPTH){vm->err=ZCE_ERR_CALL_STK;break;}
            uint32_t t=gs+(uint32_t)off; if(t>ge){vm->err=ZCE_ERR_CALL_OOB;break;}
            vm->call_stack[vm->call_depth++]=bs_tell(bs); bs_seek(bs,t); vm->steps++; continue;
        }
        if (tok == 7) {
            if(!vm->call_depth){vm->err=ZCE_ERR_RET_EMPTY;break;}
            bs_seek(bs,vm->call_stack[--vm->call_depth]); vm->steps++; continue;
        }
        if (tok>=ZCE_MAX_RULES||!vm->rules[tok].ok){vm->err=ZCE_ERR_UNDEF_TOK;break;}
        ZCE_Rule *r=&vm->rules[tok];
        int32_t args[4]={0,0,0,0};
        for(int i=0;i<r->na;i++){args[i]=bs_arg(bs,r->at[i],&vm->err);if(vm->err)return;}
        vm->steps++;
        switch(r->act){
        case 0:
            if      (r->ap==1) S_SET(vm,args[0],args[1]);
            else if (r->ap==2) S_SET(vm,args[0],S_GET(vm,args[1])+S_GET(vm,args[2]));
            else if (r->ap>=3&&r->ap<20&&vm->ext[r->ap]) vm->ext[r->ap](vm->slots,args);
            break;
        case 1: {
            int32_t cond=S_GET(vm,args[0]);
            if(!cond){
                uint32_t t=gs+(uint32_t)args[1];
                if(t>ge){vm->err=ZCE_ERR_JIF_OOB;return;}
                if(t<ipos){
                    uint32_t loop_end=bs_tell(bs);
                    ZCE_LoopBuf *lb=_find_loop(vm,t);
                    if(!lb) lb=_capture_loop(vm,bs,t,loop_end);
                    if(!lb) return;
                    while(!vm->err){
                        if(S_GET(vm,args[0])!=0) break;
                        uint32_t xt=loop_end;
                        int exited=_exec_buf(vm,lb,gs,ge,&xt);
                        if(vm->err) return;
                        if(exited){bs_seek(bs,xt);goto next;}
                    }
                } else { bs_seek(bs,t); }
            }
            break;
        }
        case 2:{uint32_t t=gs+(uint32_t)args[0];if(t>ge){vm->err=ZCE_ERR_JMP_OOB;return;}bs_seek(bs,t);break;}
        }
        next:;
    }
}
#endif /* ZCE_SEEKABLE */

/* ── Public API implementation ───────────────────────────────────────────── */

void zce_init(ZCE_VM *vm) {
    memset(vm, 0, sizeof(*vm));
    vm->rules[1]=(ZCE_Rule){1,2,{ZCE_AT_SLOT,ZCE_AT_VALUE,0,0},0,1};
    vm->rules[2]=(ZCE_Rule){1,3,{ZCE_AT_SLOT,ZCE_AT_SLOT,ZCE_AT_SLOT,0},0,2};
    vm->rules[3]=(ZCE_Rule){1,2,{ZCE_AT_SLOT,ZCE_AT_SLOT,0,0},1,0};
    vm->rules[4]=(ZCE_Rule){1,1,{ZCE_AT_SLOT,0,0,0},2,0};
    vm->rules[5]=(ZCE_Rule){1,0,{0,0,0,0},3,0};
    vm->rules[6]=(ZCE_Rule){1,1,{ZCE_AT_SLOT,0,0,0},4,0};
    vm->rules[7]=(ZCE_Rule){1,0,{0,0,0,0},5,0};
}

void zce_install_std(ZCE_VM *vm) {
    vm->ext[3]=_ext_sub; vm->ext[4]=_ext_mul; vm->ext[5]=_ext_div;
    vm->ext[6]=_ext_mod; vm->ext[7]=_ext_eq;  vm->ext[8]=_ext_lt;
    vm->ext[9]=_ext_not; vm->ext[10]=_ext_load; vm->ext[11]=_ext_stor;
}

int zce_run(ZCE_VM *vm, const uint8_t *data, uint32_t len) {
    vm->err=ZCE_OK; vm->steps=0; vm->call_depth=0;
#ifdef ZCE_SEEKABLE
    vm->_data=data; vm->_data_len=len;
#else
    vm->nloops=0;
#endif
    ZCE_BS bs; bs_init(&bs,data,len);
    uint32_t body_bits=(uint32_t)bs_elias(&bs,&vm->err);
    if(vm->err) return 0;
    uint32_t gs=bs_tell(&bs), ge=gs+body_bits;
    _run_range(vm,&bs,gs,ge);
    return vm->err==ZCE_OK ? 1 : 0;
}

const char *zce_err_str(uint8_t err) {
    switch(err){
    case ZCE_OK:            return "ok";
    case ZCE_ERR_EOF:       return "unexpected eof";
    case ZCE_ERR_ELIAS_OVF: return "elias overflow";
    case ZCE_ERR_UNDEF_TOK: return "undefined token";
    case ZCE_ERR_JIF_OOB:   return "jif out of bounds";
    case ZCE_ERR_JMP_OOB:   return "jmp out of bounds";
    case ZCE_ERR_CALL_OOB:  return "call out of bounds";
    case ZCE_ERR_CALL_STK:  return "call stack overflow";
    case ZCE_ERR_RET_EMPTY: return "ret without call";
    case ZCE_ERR_STEPS:     return "step limit exceeded";
    case ZCE_ERR_LOOP_OVF:  return "too many loops";
    case ZCE_ERR_LOOP_BIG:  return "loop body too large";
    default:                return "unknown error";
    }
}

#endif /* ZCE_IMPLEMENTATION */
#endif /* ZCE_CORE_H */
