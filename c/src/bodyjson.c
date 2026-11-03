#include "bodyjson.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

struct jnode {
    int type;
    int b;
    long long i;
    double f;
    char *str;      /* BJ_STR: UTF-8, NUL 结尾；也可含内嵌 NUL（用 len） */
    size_t slen;
    jnode **items;  /* BJ_ARR */
    size_t n;
    char **keys;    /* BJ_OBJ */
    jnode **vals;
    size_t on;
};

typedef struct {
    const char *s;
    size_t n;
    size_t i;
    char *err;
    size_t errsz;
    int depth;
} jp;

static void set_err(jp *p, const char *kind, size_t at) {
    if (!p->err || !p->errsz || p->err[0]) return;
    size_t line = 1, col = 1;
    for (size_t k = 0; k < at && k < p->n; k++) {
        if (p->s[k] == '\n') { line++; col = 1; }
        else col++;
    }
    _snprintf(p->err, p->errsz, "%s: line %zu column %zu (char %zu)",
              kind, line, col, at);
    p->err[p->errsz - 1] = 0;
}

static int utf8_error(const char *s, size_t n, size_t *pos, int *kind) {
    /* kind: 0=invalid start byte, 1=unexpected end of data, 2=invalid continuation byte */
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x80) { i++; continue; }
        int need;
        unsigned cp;
        if (c >= 0xc2 && c <= 0xdf) { need = 1; cp = c & 0x1f; }
        else if (c >= 0xe0 && c <= 0xef) { need = 2; cp = c & 0x0f; }
        else if (c >= 0xf0 && c <= 0xf4) { need = 3; cp = c & 0x07; }
        else { *pos = i; *kind = 0; return 0; }
        if (i + (size_t)need >= n) { *pos = i; *kind = 1; return 0; }
        for (int k = 1; k <= need; k++) {
            unsigned char cc = (unsigned char)s[i + (size_t)k];
            if ((cc & 0xc0) != 0x80) { *pos = i + (size_t)k; *kind = 2; return 0; }
            cp = (cp << 6) | (cc & 0x3f);
        }
        if ((need == 2 && cp < 0x800) || (need == 3 && cp < 0x10000) ||
            (cp >= 0xd800 && cp <= 0xdfff) || cp > 0x10ffff) {
            *pos = i; *kind = 2;
            return 0;
        }
        i += (size_t)need + 1;
    }
    return 1;
}

static jnode *jnew(int type) {
    jnode *v = xmalloc(sizeof(*v));
    memset(v, 0, sizeof(*v));
    v->type = type;
    return v;
}

void jfree(jnode *v) {
    if (!v) return;
    free(v->str);
    for (size_t i = 0; i < v->n; i++) jfree(v->items[i]);
    free(v->items);
    for (size_t i = 0; i < v->on; i++) {
        free(v->keys[i]);
        jfree(v->vals[i]);
    }
    free(v->keys);
    free(v->vals);
    free(v);
}

static void skip_ws(jp *p) {
    while (p->i < p->n) {
        char c = p->s[p->i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->i++;
        else break;
    }
}

static int hexv(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void put_utf8(buf *b, unsigned cp) {
    char t[4];
    if (cp < 0x80) { t[0] = (char)cp; buf_append(b, t, 1); }
    else if (cp < 0x800) {
        t[0] = (char)(0xc0 | (cp >> 6));
        t[1] = (char)(0x80 | (cp & 0x3f));
        buf_append(b, t, 2);
    } else if (cp < 0x10000) {
        t[0] = (char)(0xe0 | (cp >> 12));
        t[1] = (char)(0x80 | ((cp >> 6) & 0x3f));
        t[2] = (char)(0x80 | (cp & 0x3f));
        buf_append(b, t, 3);
    } else {
        t[0] = (char)(0xf0 | (cp >> 18));
        t[1] = (char)(0x80 | ((cp >> 12) & 0x3f));
        t[2] = (char)(0x80 | ((cp >> 6) & 0x3f));
        t[3] = (char)(0x80 | (cp & 0x3f));
        buf_append(b, t, 4);
    }
}

static int parse_hex4(jp *p, unsigned *out) {
    if (p->i + 4 > p->n) { set_err(p, "Invalid \\uXXXX escape", p->i); return 0; }
    unsigned v = 0;
    for (int k = 0; k < 4; k++) {
        int h = hexv(p->s[p->i + (size_t)k]);
        if (h < 0) { set_err(p, "Invalid \\uXXXX escape", p->i); return 0; }
        v = (v << 4) | (unsigned)h;
    }
    p->i += 4;
    *out = v;
    return 1;
}

static jnode *parse_value(jp *p);

/* 解析字符串（p->i 指向开引号），产出 BJ_STR。 */
static jnode *parse_string(jp *p) {
    size_t start = p->i;
    p->i++;                                  /* " */
    buf b;
    buf_init(&b);
    while (p->i < p->n) {
        unsigned char c = (unsigned char)p->s[p->i];
        if (c == '"') {
            p->i++;
            jnode *v = jnew(BJ_STR);
            v->str = xmalloc(b.n + 1);
            if (b.n) memcpy(v->str, b.p, b.n);
            v->str[b.n] = 0;
            v->slen = b.n;
            buf_free(&b);
            return v;
        }
        if (c < 0x20) {
            set_err(p, "Invalid control character at", p->i);
            buf_free(&b);
            return NULL;
        }
        if (c == '\\') {
            size_t esc_at = p->i;
            p->i++;
            if (p->i >= p->n) { set_err(p, "Unterminated string starting at", start); buf_free(&b); return NULL; }
            char e = p->s[p->i++];
            char ch;
            switch (e) {
            case '"': ch = '"'; break;
            case '\\': ch = '\\'; break;
            case '/': ch = '/'; break;
            case 'b': ch = '\b'; break;
            case 'f': ch = '\f'; break;
            case 'n': ch = '\n'; break;
            case 'r': ch = '\r'; break;
            case 't': ch = '\t'; break;
            case 'u': {
                unsigned cp;
                if (!parse_hex4(p, &cp)) { buf_free(&b); return NULL; }
                if (cp >= 0xd800 && cp <= 0xdbff) {
                    if (p->i + 1 < p->n && p->s[p->i] == '\\' && p->s[p->i + 1] == 'u') {
                        p->i += 2;
                        unsigned lo;
                        if (!parse_hex4(p, &lo)) { buf_free(&b); return NULL; }
                        if (lo < 0xdc00 || lo > 0xdfff) {
                            set_err(p, "Invalid \\uXXXX escape", esc_at);
                            buf_free(&b);
                            return NULL;
                        }
                        cp = 0x10000 + ((cp - 0xd800) << 10) + (lo - 0xdc00);
                    }
                }
                put_utf8(&b, cp);
                continue;
            }
            default:
                set_err(p, "Invalid \\escape", esc_at);
                buf_free(&b);
                return NULL;
            }
            buf_append(&b, &ch, 1);
            continue;
        }
        buf_append(&b, (const char *)&p->s[p->i], 1);
        p->i++;
    }
    set_err(p, "Unterminated string starting at", start);
    buf_free(&b);
    return NULL;
}

static jnode *parse_number(jp *p) {
    size_t start = p->i;
    int is_float = 0;
    if (p->i < p->n && (p->s[p->i] == '-' || p->s[p->i] == '+')) p->i++;
    size_t digits = 0;
    while (p->i < p->n && p->s[p->i] >= '0' && p->s[p->i] <= '9') { p->i++; digits++; }
    if (p->i < p->n && p->s[p->i] == '.') {
        is_float = 1;
        p->i++;
        while (p->i < p->n && p->s[p->i] >= '0' && p->s[p->i] <= '9') { p->i++; digits++; }
    }
    if (p->i < p->n && (p->s[p->i] == 'e' || p->s[p->i] == 'E')) {
        is_float = 1;
        p->i++;
        if (p->i < p->n && (p->s[p->i] == '-' || p->s[p->i] == '+')) p->i++;
        while (p->i < p->n && p->s[p->i] >= '0' && p->s[p->i] <= '9') p->i++;
    }
    if (!digits) {
        p->i = start;
        set_err(p, "Expecting value", start);
        return NULL;
    }
    char tmp[128];
    size_t len = p->i - start;
    if (len >= sizeof(tmp)) len = sizeof(tmp) - 1;
    memcpy(tmp, p->s + start, len);
    tmp[len] = 0;
    jnode *v;
    if (is_float) {
        v = jnew(BJ_FLOAT);
        v->f = strtod(tmp, NULL);
    } else {
        v = jnew(BJ_INT);
        errno = 0;
        long long x = strtoll(tmp, NULL, 10);
        v->i = x;                            /* 超 int64 饱和（已知差异） */
    }
    return v;
}

static jnode *parse_array(jp *p) {
    p->i++;                                  /* [ */
    jnode *v = jnew(BJ_ARR);
    skip_ws(p);
    if (p->i < p->n && p->s[p->i] == ']') { p->i++; return v; }
    for (;;) {
        jnode *it = parse_value(p);
        if (!it) { jfree(v); return NULL; }
        v->items = xrealloc(v->items, (v->n + 1) * sizeof(*v->items));
        v->items[v->n++] = it;
        skip_ws(p);
        if (p->i >= p->n) { set_err(p, "Expecting ',' delimiter", p->i); jfree(v); return NULL; }
        if (p->s[p->i] == ',') { p->i++; skip_ws(p); continue; }
        if (p->s[p->i] == ']') { p->i++; return v; }
        set_err(p, "Expecting ',' delimiter", p->i);
        jfree(v);
        return NULL;
    }
}

static jnode *parse_object(jp *p) {
    p->i++;                                  /* { */
    jnode *v = jnew(BJ_OBJ);
    skip_ws(p);
    if (p->i < p->n && p->s[p->i] == '}') { p->i++; return v; }
    for (;;) {
        skip_ws(p);
        if (p->i >= p->n || p->s[p->i] != '"') {
            set_err(p, "Expecting property name enclosed in double quotes", p->i);
            jfree(v);
            return NULL;
        }
        jnode *k = parse_string(p);
        if (!k) { jfree(v); return NULL; }
        skip_ws(p);
        if (p->i >= p->n || p->s[p->i] != ':') {
            set_err(p, "Expecting ':' delimiter", p->i);
            jfree(k);
            jfree(v);
            return NULL;
        }
        p->i++;
        skip_ws(p);
        jnode *val = parse_value(p);
        if (!val) { jfree(k); jfree(v); return NULL; }
        v->keys = xrealloc(v->keys, (v->on + 1) * sizeof(*v->keys));
        v->vals = xrealloc(v->vals, (v->on + 1) * sizeof(*v->vals));
        v->keys[v->on] = xstrdup(k->str ? k->str : "");
        v->vals[v->on] = val;
        v->on++;
        jfree(k);
        skip_ws(p);
        if (p->i >= p->n) { set_err(p, "Expecting ',' delimiter", p->i); jfree(v); return NULL; }
        if (p->s[p->i] == ',') { p->i++; continue; }
        if (p->s[p->i] == '}') { p->i++; return v; }
        set_err(p, "Expecting ',' delimiter", p->i);
        jfree(v);
        return NULL;
    }
}

static int match_lit(jp *p, const char *lit) {
    size_t l = strlen(lit);
    if (p->i + l > p->n) return 0;
    if (memcmp(p->s + p->i, lit, l)) return 0;
    p->i += l;
    return 1;
}

static jnode *parse_value(jp *p) {
    if (p->depth > 64) { set_err(p, "Maximum nesting depth exceeded", p->i); return NULL; }
    skip_ws(p);
    if (p->i >= p->n) { set_err(p, "Expecting value", p->i); return NULL; }
    char c = p->s[p->i];
    if (c == '"') return parse_string(p);
    if (c == '{') { p->depth++; jnode *v = parse_object(p); p->depth--; return v; }
    if (c == '[') { p->depth++; jnode *v = parse_array(p); p->depth--; return v; }
    if (c == 't' && match_lit(p, "true")) { jnode *v = jnew(BJ_BOOL); v->b = 1; return v; }
    if (c == 'f' && match_lit(p, "false")) { jnode *v = jnew(BJ_BOOL); v->b = 0; return v; }
    if (c == 'n' && match_lit(p, "null")) return jnew(BJ_NULL);
    /* Python 的 json.loads 默认接受 NaN / Infinity / -Infinity */
    if (c == 'N' && match_lit(p, "NaN")) {
        jnode *v = jnew(BJ_FLOAT);
        v->f = NAN;
        return v;
    }
    if (c == 'I' && match_lit(p, "Infinity")) {
        jnode *v = jnew(BJ_FLOAT);
        v->f = INFINITY;
        return v;
    }
    if (c == '-' && p->i + 9 <= p->n && !memcmp(p->s + p->i, "-Infinity", 9)) {
        p->i += 9;
        jnode *v = jnew(BJ_FLOAT);
        v->f = -INFINITY;
        return v;
    }
    if (c == '-' || (c >= '0' && c <= '9')) return parse_number(p);
    set_err(p, "Expecting value", p->i);
    return NULL;
}

jnode *jparse(const char *s, size_t n, char *err, size_t errsz) {
    if (err && errsz) err[0] = 0;
    if (!s) s = "";
    {
        size_t bad_pos = 0;
        int kind = 0;
        if (!utf8_error(s, n, &bad_pos, &kind)) {
            if (err && errsz) {
                unsigned char bad = (unsigned char)s[bad_pos < n ? bad_pos : 0];
                const char *why = kind == 0 ? "invalid start byte"
                                : kind == 1 ? "unexpected end of data"
                                            : "invalid continuation byte";
                _snprintf(err, errsz,
                          "'utf-8' codec can't decode byte 0x%02x in position %zu: %s",
                          (unsigned)bad, bad_pos, why);
                err[errsz - 1] = 0;
            }
            return NULL;
        }
    }
    jp p;
    memset(&p, 0, sizeof(p));
    p.s = s;
    p.n = n;
    p.err = err;
    p.errsz = errsz;
    jnode *v = parse_value(&p);
    if (!v) return NULL;
    skip_ws(&p);
    if (p.i < p.n) {
        set_err(&p, "Extra data", p.i);
        jfree(v);
        return NULL;
    }
    return v;
}

int bjtype(const jnode *v) { return v ? v->type : BJ_NULL; }

const char *jtype_name(const jnode *v) {
    switch (bjtype(v)) {
    case BJ_NULL: return "NoneType";
    case BJ_BOOL: return "bool";
    case BJ_INT: return "int";
    case BJ_FLOAT: return "float";
    case BJ_STR: return "str";
    case BJ_ARR: return "list";
    case BJ_OBJ: return "dict";
    default: return "NoneType";
    }
}

int jis_str(const jnode *v) { return v && v->type == BJ_STR; }
const char *jstr(const jnode *v) { return jis_str(v) ? v->str : NULL; }
size_t jstrlen(const jnode *v) { return jis_str(v) ? v->slen : 0; }
int jis_bool(const jnode *v) { return v && v->type == BJ_BOOL; }
int jbool(const jnode *v) { return jis_bool(v) ? v->b : 0; }
int jis_int(const jnode *v) { return v && v->type == BJ_INT; }
int jis_float(const jnode *v) { return v && v->type == BJ_FLOAT; }
long long jint(const jnode *v) { return jis_int(v) ? v->i : 0; }
double jfloat(const jnode *v) { return jis_float(v) ? v->f : 0.0; }
size_t jlen(const jnode *v) {
    if (!v) return 0;
    if (v->type == BJ_ARR) return v->n;
    if (v->type == BJ_OBJ) return v->on;
    return 0;
}
const jnode *jat(const jnode *v, size_t i) {
    if (!v || v->type != BJ_ARR || i >= v->n) return NULL;
    return v->items[i];
}
const jnode *jget(const jnode *v, const char *key) {
    if (!v || v->type != BJ_OBJ || !key) return NULL;
    for (size_t i = v->on; i > 0; i--)          /* Python dict 后写覆盖前写 */
        if (!strcmp(v->keys[i - 1], key)) return v->vals[i - 1];
    return NULL;
}
const char *jkey(const jnode *v, size_t i) {
    if (!v || v->type != BJ_OBJ || i >= v->on) return NULL;
    return v->keys[i];
}

int jhas(const jnode *v, const char *key) {
    return jget(v, key) != NULL;
}
