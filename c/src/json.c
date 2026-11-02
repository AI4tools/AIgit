#include "json.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------- 写 */

void json_str(buf *o, const char *s) {
    buf_putc(o, '"');
    if (!s) { buf_putc(o, '"'); return; }
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        unsigned c = *p;
        switch (c) {
        case '"':  buf_puts(o, "\\\""); break;
        case '\\': buf_puts(o, "\\\\"); break;
        case '\n': buf_puts(o, "\\n"); break;
        case '\r': buf_puts(o, "\\r"); break;
        case '\t': buf_puts(o, "\\t"); break;
        case '\b': buf_puts(o, "\\b"); break;
        case '\f': buf_puts(o, "\\f"); break;
        default:
            if (c < 0x20) buf_printf(o, "\\u%04x", c);
            else buf_putc(o, (char)c);   /* 字节原样透传：路径/文本都是 UTF-8 */
        }
    }
    buf_putc(o, '"');
}

void json_kv_str(buf *o, const char *k, const char *v, int comma) {
    json_str(o, k);
    buf_putc(o, ':');
    json_str(o, v);
    if (comma) buf_putc(o, ',');
}

void json_kv_int(buf *o, const char *k, long long v, int comma) {
    json_str(o, k);
    buf_printf(o, ":%lld", v);
    if (comma) buf_putc(o, ',');
}

void json_kv_bool(buf *o, const char *k, int v, int comma) {
    json_str(o, k);
    buf_puts(o, v ? ":true" : ":false");
    if (comma) buf_putc(o, ',');
}

/* ---------------------------------------------------------------- 读 */

typedef struct {
    const char *s;
    size_t      n;
    size_t      i;
    int         depth;
    const char *err;
} jp;

static void j_ws(jp *p) {
    while (p->i < p->n) {
        char c = p->s[p->i];
        if (c == ' ' || c == '\t' || c == '\n' || c == '\r') p->i++;
        else break;
    }
}

static int j_hex(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static char *j_string(jp *p) {
    if (p->i >= p->n || p->s[p->i] != '"') { p->err = "expect string"; return NULL; }
    p->i++;
    buf b;
    buf_init(&b);
    while (p->i < p->n) {
        unsigned char c = (unsigned char)p->s[p->i++];
        if (c == '"') { return buf_cstr(&b) ? (char *)b.p : xstrdup(""); }
        if (c == '\\') {
            if (p->i >= p->n) break;
            char e = p->s[p->i++];
            switch (e) {
            case '"': buf_putc(&b, '"'); break;
            case '\\': buf_putc(&b, '\\'); break;
            case '/': buf_putc(&b, '/'); break;
            case 'b': buf_putc(&b, '\b'); break;
            case 'f': buf_putc(&b, '\f'); break;
            case 'n': buf_putc(&b, '\n'); break;
            case 'r': buf_putc(&b, '\r'); break;
            case 't': buf_putc(&b, '\t'); break;
            case 'u': {
                if (p->i + 4 > p->n) { p->err = "bad \\u"; buf_free(&b); return NULL; }
                int h0 = j_hex(p->s[p->i]), h1 = j_hex(p->s[p->i + 1]);
                int h2 = j_hex(p->s[p->i + 2]), h3 = j_hex(p->s[p->i + 3]);
                if (h0 < 0 || h1 < 0 || h2 < 0 || h3 < 0) { p->err = "bad \\u"; buf_free(&b); return NULL; }
                p->i += 4;
                unsigned cp = (unsigned)((h0 << 12) | (h1 << 8) | (h2 << 4) | h3);
                if (cp < 0x80) buf_putc(&b, (char)cp);
                else if (cp < 0x800) {
                    buf_putc(&b, (char)(0xC0 | (cp >> 6)));
                    buf_putc(&b, (char)(0x80 | (cp & 0x3F)));
                } else {
                    buf_putc(&b, (char)(0xE0 | (cp >> 12)));
                    buf_putc(&b, (char)(0x80 | ((cp >> 6) & 0x3F)));
                    buf_putc(&b, (char)(0x80 | (cp & 0x3F)));
                }
                break;
            }
            default: p->err = "bad escape"; buf_free(&b); return NULL;
            }
            continue;
        }
        buf_putc(&b, (char)c);
    }
    p->err = "unterminated string";
    buf_free(&b);
    return NULL;
}

static jval *j_new(jtype t) {
    jval *v = xmalloc(sizeof(*v));
    memset(v, 0, sizeof(*v));
    v->t = t;
    return v;
}

static void j_push(jval *v, char *key, jval *item) {
    v->items = xrealloc(v->items, (v->n + 1) * sizeof(jval *));
    if (v->t == J_OBJ) v->keys = xrealloc(v->keys, (v->n + 1) * sizeof(char *));
    v->items[v->n] = item;
    if (v->t == J_OBJ) v->keys[v->n] = key;
    v->n++;
}

static jval *j_parse_value(jp *p) {
    j_ws(p);
    if (p->i >= p->n) { p->err = "unexpected end"; return NULL; }
    char c = p->s[p->i];
    if (c == '{' || c == '[') {
        if (++p->depth > 64) { p->err = "nesting too deep"; return NULL; }
        int is_obj = (c == '{');
        jval *v = j_new(is_obj ? J_OBJ : J_ARR);
        p->i++;
        j_ws(p);
        if (p->i < p->n && p->s[p->i] == (is_obj ? '}' : ']')) { p->i++; p->depth--; return v; }
        while (1) {
            char *key = NULL;
            if (is_obj) {
                j_ws(p);
                key = j_string(p);
                if (!key) { jval_free(v); return NULL; }
                j_ws(p);
                if (p->i >= p->n || p->s[p->i] != ':') { p->err = "expect ':'"; free(key); jval_free(v); return NULL; }
                p->i++;
            }
            jval *item = j_parse_value(p);
            if (!item) { free(key); jval_free(v); return NULL; }
            j_push(v, key, item);
            j_ws(p);
            if (p->i < p->n && p->s[p->i] == ',') { p->i++; continue; }
            if (p->i < p->n && p->s[p->i] == (is_obj ? '}' : ']')) { p->i++; break; }
            p->err = "expect ',' or close";
            jval_free(v);
            return NULL;
        }
        p->depth--;
        return v;
    }
    if (c == '"') {
        char *s = j_string(p);
        if (!s) return NULL;
        jval *v = j_new(J_STR);
        v->str = s;
        return v;
    }
    if (c == 't' && p->i + 4 <= p->n && !memcmp(p->s + p->i, "true", 4)) {
        p->i += 4;
        jval *v = j_new(J_BOOL);
        v->b = 1;
        return v;
    }
    if (c == 'f' && p->i + 5 <= p->n && !memcmp(p->s + p->i, "false", 5)) {
        p->i += 5;
        jval *v = j_new(J_BOOL);
        v->b = 0;
        return v;
    }
    if (c == 'n' && p->i + 4 <= p->n && !memcmp(p->s + p->i, "null", 4)) {
        p->i += 4;
        return j_new(J_NULL);
    }
    if (c == '-' || (c >= '0' && c <= '9')) {
        size_t start = p->i;
        if (p->s[p->i] == '-') p->i++;
        while (p->i < p->n && ((p->s[p->i] >= '0' && p->s[p->i] <= '9') || p->s[p->i] == '.' ||
                               p->s[p->i] == 'e' || p->s[p->i] == 'E' || p->s[p->i] == '+' || p->s[p->i] == '-'))
            p->i++;
        char tmp[64];
        size_t len = p->i - start;
        if (len >= sizeof(tmp)) { p->err = "number too long"; return NULL; }
        memcpy(tmp, p->s + start, len);
        tmp[len] = 0;
        char *end = NULL;
        double d = strtod(tmp, &end);
        if (end == tmp) { p->err = "bad number"; return NULL; }
        jval *v = j_new(J_NUM);
        v->num = d;
        return v;
    }
    p->err = "unexpected character";
    return NULL;
}

jval *json_parse(const char *s, size_t len, const char **err) {
    jp p = { s, len, 0, 0, NULL };
    jval *v = j_parse_value(&p);
    if (!v) { if (err) *err = p.err ? p.err : "parse error"; return NULL; }
    j_ws(&p);
    if (p.i != p.n) { if (err) *err = "trailing garbage"; jval_free(v); return NULL; }
    return v;
}

void jval_free(jval *v) {
    if (!v) return;
    for (size_t i = 0; i < v->n; i++) {
        if (v->t == J_OBJ && v->keys) free(v->keys[i]);
        jval_free(v->items[i]);
    }
    free(v->items);
    free(v->keys);
    free(v->str);
    free(v);
}

jval *json_get(const jval *obj, const char *key) {
    if (!obj || obj->t != J_OBJ) return NULL;
    for (size_t i = 0; i < obj->n; i++)
        if (obj->keys[i] && !strcmp(obj->keys[i], key)) return obj->items[i];
    return NULL;
}

const char *json_str_of(const jval *obj, const char *key, const char *dflt) {
    jval *v = json_get(obj, key);
    return (v && v->t == J_STR) ? v->str : dflt;
}

long long json_int_of(const jval *obj, const char *key, long long dflt) {
    jval *v = json_get(obj, key);
    if (!v) return dflt;
    if (v->t == J_NUM) return (long long)v->num;
    return dflt;
}

int json_bool_of(const jval *obj, const char *key, int dflt) {
    jval *v = json_get(obj, key);
    return (v && v->t == J_BOOL) ? v->b : dflt;
}
