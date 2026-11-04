#include "status.h"

#include <stdlib.h>
#include <string.h>

#include "git.h"
#include "json.h"

static void item_push(sitem **arr, size_t *n, const char *path, size_t plen,
                      char code, int staged) {
    *arr = xrealloc(*arr, (*n + 1) * sizeof(sitem));
    sitem *it = &(*arr)[*n];
    it->path = xstrndup(path, plen);
    it->code = code;
    it->staged = staged;
    (*n)++;
}

/* v2 的 XY 里 '.' 表示「未改动」，归一成空格（与 Python _v2_xy 一致） */
static void v2_xy(const char *xy, char *x, char *y) {
    *x = xy[0] ? (xy[0] == '.' ? ' ' : xy[0]) : ' ';
    *y = xy[1] ? (xy[1] == '.' ? ' ' : xy[1]) : ' ';
}

/* 取第 idx 个以单个空格分隔的字段；最后一个字段取到串尾（路径里可以有空格）。 */
static int nth_field(const char *s, int idx, const char **out, size_t *len) {
    const char *p = s;
    for (int i = 0; i < idx; i++) {
        const char *sp = strchr(p, ' ');
        if (!sp) return 0;
        p = sp + 1;
    }
    const char *end = strchr(p, ' ');
    *out = p;
    *len = end ? (size_t)(end - p) : strlen(p);
    return 1;
}

/* 取第 idx 个字段之后的**剩余整段**（porcelain v2 的路径字段永远是最后一段，
 * 路径里可以有空格；按空格切会把 "with space.txt" 截成 "with"）。 */
static int nth_rest(const char *s, int idx, const char **out, size_t *len) {
    const char *p = s;
    for (int i = 0; i < idx; i++) {
        const char *sp = strchr(p, ' ');
        if (!sp) return 0;
        p = sp + 1;
    }
    *out = p;
    *len = strlen(p);
    return 1;
}

int status_compute(status_t *st, char **err) {
    memset(st, 0, sizeof(*st));
    buf out, er;
    buf_init(&out);
    buf_init(&er);
    const char *args[] = { "status", "--porcelain=v2", "--branch", "-z",
                           "--untracked-files=all", NULL };
    int rc = git_run(args, NULL, 0, &out, &er);
    if (rc != 0) {
        if (err) {
            buf m;
            buf_init(&m);
            buf_printf(&m, "git status 失败: %s", buf_cstr(&er));
            *err = xstrdup(buf_cstr(&m));
            buf_free(&m);
        }
        buf_free(&out);
        buf_free(&er);
        return -1;
    }
    buf_free(&er);

    char *s = buf_cstr(&out);
    size_t len = out.n;
    size_t pos = 0;
    int skip_next = 0;
    while (pos < len) {
        const char *f = s + pos;
        size_t flen = 0; while (flen < len - pos && f[flen]) flen++;
        pos += flen + 1;
        if (flen == 0) continue;
        if (skip_next) { skip_next = 0; continue; }

        if (f[0] == '#') {
            static const char *bh = "# branch.head ";
            size_t bh_len = strlen(bh);
            if (flen > bh_len && !strncmp(f, bh, bh_len)) {
                const char *b = f + bh_len;
                size_t blen = flen - bh_len;
                free(st->branch);
                if (blen == 10 && !strncmp(b, "(detached)", 10)) st->branch = xstrdup("HEAD");
                else st->branch = xstrndup(b, blen);
            }
            continue;
        }
        if (f[0] == '?') {
            const char *p = (flen > 1 && f[1] == ' ') ? f + 2 : f + 1;
            size_t plen = (size_t)(f + flen - p);
            item_push(&st->unstaged, &st->nu, p, plen, '?', 0);
            continue;
        }
        if (f[0] == '!') continue;

        const char *xy = NULL;
        size_t xylen = 0;
        const char *path = NULL;
        size_t plen = 0;
        if (f[0] == '1') {
            if (!nth_field(f, 1, &xy, &xylen) || !nth_rest(f, 8, &path, &plen)) continue;
        } else if (f[0] == '2') {
            if (!nth_field(f, 1, &xy, &xylen) || !nth_rest(f, 9, &path, &plen)) continue;
            skip_next = 1;                      /* 下一字段是 origPath */
        } else if (f[0] == 'u') {
            if (!nth_field(f, 1, &xy, &xylen) || !nth_rest(f, 10, &path, &plen)) continue;
            item_push(&st->conf, &st->nc, path, plen, 'U', 1);
        } else {
            continue;
        }
        char x, y;
        char xy2[2] = { xylen > 0 ? xy[0] : 0, xylen > 1 ? xy[1] : 0 };
        v2_xy(xy2, &x, &y);
        if (x != ' ' && x != '?') item_push(&st->staged, &st->ns, path, plen, x, 1);
        if (y != ' ' && y != '?') item_push(&st->unstaged, &st->nu, path, plen, y, 0);
    }
    buf_free(&out);
    if (!st->branch) st->branch = xstrdup("");
    return 0;
}

void status_free(status_t *st) {
    free(st->branch);
    for (size_t i = 0; i < st->ns; i++) free(st->staged[i].path);
    for (size_t i = 0; i < st->nu; i++) free(st->unstaged[i].path);
    for (size_t i = 0; i < st->nc; i++) free(st->conf[i].path);
    free(st->staged);
    free(st->unstaged);
    free(st->conf);
    memset(st, 0, sizeof(*st));
}

static void items_json(buf *o, const sitem *arr, size_t n, int staged_side) {
    buf_putc(o, '[');
    for (size_t i = 0; i < n; i++) {
        if (i) buf_putc(o, ',');
        buf_puts(o, "{\"path\":");
        json_str(o, arr[i].path);
        if (staged_side) {
            buf_puts(o, ",\"staged\":true,\"status\":");
            char c[2] = { arr[i].code, 0 };
            json_str(o, c);
        } else {
            buf_puts(o, ",\"staged\":false,\"status\":");
            char c[2] = { arr[i].code, 0 };
            json_str(o, c);
        }
        buf_putc(o, '}');
    }
    buf_putc(o, ']');
}

void status_json_fields(buf *o, const status_t *st, const char *root_u8) {
    buf_puts(o, "\"branch\":");
    json_str(o, st->branch ? st->branch : "");
    buf_puts(o, ",\"root\":");
    json_str(o, root_u8);
    buf_puts(o, ",\"staged\":");
    items_json(o, st->staged, st->ns, 1);
    buf_puts(o, ",\"unstaged\":");
    items_json(o, st->unstaged, st->nu, 0);
    buf_puts(o, ",\"conflicted\":");
    buf_puts(o, "[");
    for (size_t i = 0; i < st->nc; i++) {
        if (i) buf_putc(o, ',');
        buf_puts(o, "{\"path\":");
        json_str(o, st->conf[i].path);
        buf_puts(o, ",\"status\":\"U\"}");
    }
    buf_puts(o, "]");
    buf_printf(o, ",\"hasStaged\":%s", st->ns ? "true" : "false");
}

void status_json(buf *o, const status_t *st, const char *root_u8) {
    buf_puts(o, "{\"ok\":true,");
    status_json_fields(o, st, root_u8);
    buf_putc(o, '}');
}
