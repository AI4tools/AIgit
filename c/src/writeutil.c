#include "writeutil.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "git.h"
#include "http.h"
#include "json.h"
#include "status.h"

static void send_bad(SOCKET s, const char *msg) {
    http_send_err(s, 400, msg, NULL);
}

jnode *wbody_parse(SOCKET s, const char *body, size_t len) {
    char err[256];
    if (!len) {
        jnode *v = jparse("{}", 2, err, sizeof(err));
        if (!v) send_bad(s, "服务端内部错误,详情见服务端日志");
        return v;
    }
    jnode *v = jparse(body, len, err, sizeof(err));
    if (!v) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "请求参数不合法:");
        buf_puts(&m, err);
        send_bad(s, buf_cstr(&m));
        buf_free(&m);
        return NULL;
    }
    if (bjtype(v) != BJ_OBJ) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "请求体必须是 JSON 对象,收到 ");
        buf_puts(&m, jtype_name(v));
        send_bad(s, buf_cstr(&m));
        buf_free(&m);
        jfree(v);
        return NULL;
    }
    return v;
}

static void repr_float(double d, buf *out) {
    if (isnan(d)) { buf_puts(out, "nan"); return; }
    if (isinf(d)) { buf_puts(out, d > 0 ? "inf" : "-inf"); return; }
    char t[64];
    for (int p = 1; p <= 17; p++) {
        snprintf(t, sizeof(t), "%.*g", p, d);
        if (strtod(t, NULL) == d) break;
    }
    /* Python 的 repr 对整数值浮点保留 .0；指数形式保持 %g 的 e+XX */
    if (!strchr(t, '.') && !strchr(t, 'e') && !strchr(t, 'E')) {
        size_t n = strlen(t);
        if (n + 3 < sizeof(t)) { t[n] = '.'; t[n + 1] = '0'; t[n + 2] = 0; }
    }
    buf_puts(out, t);
}

void w_repr(const jnode *v, buf *out) {
    switch (bjtype(v)) {
    case BJ_NULL: buf_puts(out, "None"); break;
    case BJ_BOOL: buf_puts(out, jbool(v) ? "True" : "False"); break;
    case BJ_INT: buf_printf(out, "%lld", jint(v)); break;
    case BJ_FLOAT: repr_float(jfloat(v), out); break;
    case BJ_STR: {
        buf_putc(out, '\'');
        const unsigned char *p = (const unsigned char *)jstr(v);
        size_t n = jstrlen(v);
        for (size_t i = 0; i < n; i++) {
            unsigned char c = p[i];
            if (c == '\\') buf_puts(out, "\\\\");
            else if (c == '\'') buf_puts(out, "\\'");
            else if (c == '\n') buf_puts(out, "\\n");
            else if (c == '\r') buf_puts(out, "\\r");
            else if (c == '\t') buf_puts(out, "\\t");
            else if (c < 0x20 || c == 0x7f) buf_printf(out, "\\x%02x", c);
            else buf_append(out, (const char *)&p[i], 1);
        }
        buf_putc(out, '\'');
        break;
    }
    case BJ_ARR:
        buf_putc(out, '[');
        for (size_t i = 0; i < jlen(v); i++) {
            if (i) buf_puts(out, ", ");
            w_repr(jat(v, i), out);
        }
        buf_putc(out, ']');
        break;
    case BJ_OBJ:
        buf_putc(out, '{');
        for (size_t i = 0; i < jlen(v); i++) {
            if (i) buf_puts(out, ", ");
            buf_putc(out, '\'');
            const char *k = jkey(v, i);
            for (const char *q = k ? k : ""; *q; q++) {
                if (*q == '\\') buf_puts(out, "\\\\");
                else if (*q == '\'') buf_puts(out, "\\'");
                else buf_putc(out, *q);
            }
            buf_puts(out, "': ");
            w_repr(jget(v, k ? k : ""), out);
        }
        buf_putc(out, '}');
        break;
    default: buf_puts(out, "None"); break;
    }
}

int wbody_int(SOCKET s, const jnode *b, const char *key, long long dflt, long long *out) {
    const jnode *v = jget(b, key);
    if (!v) { *out = dflt; return 1; }
    if (jis_int(v)) { *out = jint(v); return 1; }
    if (jis_float(v)) { *out = (long long)jfloat(v); return 1; }
    if (jis_str(v)) {
        const char *t = jstr(v);
        char *end = NULL;
        long long x = strtoll(t, &end, 10);
        if (end && *end == 0 && end != t) { *out = x; return 1; }
    }
    buf m;
    buf_init(&m);
    buf_puts(&m, "参数 ");
    buf_puts(&m, key);
    buf_puts(&m, " 必须是整数,收到 ");
    w_repr(v, &m);
    send_bad(s, buf_cstr(&m));
    buf_free(&m);
    return 0;
}

int wbody_bool(SOCKET s, const jnode *b, const char *key, int dflt, int *out) {
    const jnode *v = jget(b, key);
    if (!v) { *out = dflt; return 1; }
    if (jis_bool(v)) { *out = jbool(v); return 1; }
    buf m;
    buf_init(&m);
    buf_puts(&m, key);
    buf_puts(&m, " 必须是 true 或 false,收到 ");
    w_repr(v, &m);
    send_bad(s, buf_cstr(&m));
    buf_free(&m);
    return 0;
}

void wbody_free_files(char **v, size_t n) {
    for (size_t i = 0; i < n; i++) free(v[i]);
    free(v);
}

int wbody_files(SOCKET s, const jnode *b, char ***out, size_t *n) {
    *out = NULL;
    *n = 0;
    const jnode *v = jget(b, "files");
    if (!v || bjtype(v) == BJ_NULL) return 1;
    if (bjtype(v) != BJ_ARR) {
        buf m;
        buf_init(&m);
        buf_puts(&m, "files 必须是字符串数组,收到 ");
        buf_puts(&m, jtype_name(v));
        send_bad(s, buf_cstr(&m));
        buf_free(&m);
        return 0;
    }
    size_t cnt = jlen(v);
    char **arr = cnt ? xmalloc(cnt * sizeof(*arr)) : NULL;
    for (size_t i = 0; i < cnt; i++) {
        const jnode *it = jat(v, i);
        if (!jis_str(it)) {
            buf m;
            buf_init(&m);
            buf_puts(&m, "files 里每一项都必须是字符串,收到 ");
            buf_puts(&m, jtype_name(it));
            send_bad(s, buf_cstr(&m));
            buf_free(&m);
            wbody_free_files(arr, i);
            return 0;
        }
        arr[i] = xstrdup(jstr(it));
    }
    *out = arr;
    *n = cnt;
    return 1;
}

int wbody_status_omitted(const jnode *b) {
    const jnode *v = jget(b, "status");
    return jis_bool(v) && !jbool(v);
}

void wsend_ok(SOCKET s, const jnode *b, const char *extra_json) {
    buf o;
    buf_init(&o);
    if (wbody_status_omitted(b)) {
        buf_puts(&o, "{\"ok\":true");
        if (extra_json && *extra_json) {
            buf_putc(&o, ',');
            buf_puts(&o, extra_json);
        }
        buf_puts(&o, ",\"statusOmitted\":true}");
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
        return;
    }

    status_t st;
    char *err = NULL;
    int rc = status_compute(&st, &err);
    buf_puts(&o, "{\"ok\":true");
    if (extra_json && *extra_json) {
        buf_putc(&o, ',');
        buf_puts(&o, extra_json);
    }
    if (rc != 0) {
        /* E3 兜底：操作已经做完了，不能报 400。 */
        buf_puts(&o, ",\"branch\":null,\"staged\":[],\"unstaged\":[],"
                     "\"conflicted\":[],\"hasStaged\":false,\"root\":");
        char *root = w2u8(git_root());
        json_str(&o, root ? root : "");
        free(root);
        buf_puts(&o, ",\"statusError\":");
        buf m;
        buf_init(&m);
        buf_puts(&m, "GitError: ");
        buf_puts(&m, err ? err : "git status 失败");
        json_str(&o, buf_cstr(&m));
        buf_free(&m);
        buf_putc(&o, '}');
        http_send_json(s, 200, buf_cstr(&o), o.n);
        buf_free(&o);
        free(err);
        return;
    }
    buf_putc(&o, ',');
    char *root = w2u8(git_root());
    status_json_fields(&o, &st, root ? root : "");
    free(root);
    buf_putc(&o, '}');
    status_free(&st);
    http_send_json(s, 200, buf_cstr(&o), o.n);
    buf_free(&o);
}
