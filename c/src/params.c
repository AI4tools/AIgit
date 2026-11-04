#include "params.h"

#include <ctype.h>
#include <stdlib.h>
#include <string.h>

#include "util.h"

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

/* 在 query 里找 key，把值 percent-decode 成**带长度**的字节串（可含 NUL）。
 * 返回 1=找到（*out 为 malloc，调用方 free），0=没有，-1=内存不足。 */
int q_get_n(const char *query, const char *key, char **out, size_t *outlen) {
    if (out) *out = NULL;
    if (outlen) *outlen = 0;
    if (!query || !key) return 0;
    size_t klen = strlen(key);
    const char *p = query;
    while (p && *p) {
        const char *amp = strchr(p, '&');
        const char *end = amp ? amp : p + strlen(p);
        const char *eq = memchr(p, '=', (size_t)(end - p));
        size_t this_klen = eq ? (size_t)(eq - p) : (size_t)(end - p);
        if (this_klen == klen && !strncmp(p, key, klen)) {
            const char *v = eq ? eq + 1 : end;
            /* 解码后绝不会比原文更长 */
            char *o = xmalloc((size_t)(end - v) + 1);
            size_t oi = 0;
            for (const char *q = v; q < end; q++) {
                char c = *q;
                if (c == '+') c = ' ';
                else if (c == '%' && q + 2 < end) {
                    int h = hexval(q[1]), l = hexval(q[2]);
                    if (h >= 0 && l >= 0) { c = (char)((h << 4) | l); q += 2; }
                }
                o[oi++] = c;
            }
            o[oi] = 0;
            if (out) *out = o; else free(o);
            if (outlen) *outlen = oi;
            return 1;
        }
        p = amp ? amp + 1 : NULL;
    }
    return 0;
}

int q_get_nonempty(const char *query, const char *key, char **out, size_t *outlen) {
    int r = q_get_n(query, key, out, outlen);
    if (r == 1 && *outlen == 0) {
        free(*out);
        *out = NULL;
        return 0;
    }
    return r;
}

/* 与 Python 版的对齐点：
 *   * 缺参数 → 0；参数非整数 → -1（调用方回 400），**不静默取默认**；
 *   * 解码结果含 NUL → -2（不能截断成另一个名字，那是路径走私）；
 *   * `+` 在 query 里是空格（urllib 同语义）；%XX 非法 → 原样保留。 */
int q_get(const char *query, const char *key, char *out, size_t outsz) {
    char *v = NULL;
    size_t n = 0;
    int got = q_get_n(query, key, &v, &n);
    if (got != 1) return got;
    if (memchr(v, '\0', n)) { free(v); return -2; }   /* 内嵌 NUL：拒绝，不截断 */
    if (n + 1 > outsz) { free(v); return -1; }
    memcpy(out, v, n + 1);
    free(v);
    return 1;
}

/* Unicode Nd（decimal digit）区间：value = (cp - start) % 10。
 * 由 Python unicodedata.decimal 生成，62 段 / 660 个码点。 */
typedef struct { unsigned cp, count; } nd_range;
static const nd_range g_nd[] = {
    {0x30,10},{0x660,10},{0x6F0,10},{0x7C0,10},{0x966,10},{0x9E6,10},
    {0xA66,10},{0xAE6,10},{0xB66,10},{0xBE6,10},{0xC66,10},{0xCE6,10},
    {0xD66,10},{0xDE6,10},{0xE50,10},{0xED0,10},{0xF20,10},{0x1040,10},
    {0x1090,10},{0x17E0,10},{0x1810,10},{0x1946,10},{0x19D0,10},{0x1A80,10},
    {0x1A90,10},{0x1B50,10},{0x1BB0,10},{0x1C40,10},{0x1C50,10},{0xA620,10},
    {0xA8D0,10},{0xA900,10},{0xA9D0,10},{0xA9F0,10},{0xAA50,10},{0xABF0,10},
    {0xFF10,10},{0x104A0,10},{0x10D30,10},{0x11066,10},{0x110F0,10},
    {0x11136,10},{0x111D0,10},{0x112F0,10},{0x11450,10},{0x114D0,10},
    {0x11650,10},{0x116C0,10},{0x11730,10},{0x118E0,10},{0x11950,10},
    {0x11C50,10},{0x11D50,10},{0x11DA0,10},{0x16A60,10},{0x16AC0,10},
    {0x16B50,10},{0x1D7CE,50},{0x1E140,10},{0x1E2F0,10},{0x1E950,10},
    {0x1FBF0,10}
};

static int uni_decimal(unsigned cp, int *v) {
    for (size_t i = 0; i < sizeof(g_nd) / sizeof(g_nd[0]); i++) {
        if (cp >= g_nd[i].cp && cp < g_nd[i].cp + g_nd[i].count) {
            *v = (int)((cp - g_nd[i].cp) % 10);
            return 1;
        }
    }
    return 0;
}

int py_is_ascii_space(int c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\v' ||
           c == '\f';
}

int py_utf8_step(const unsigned char *s, size_t n, size_t *i, unsigned *cp) {
    int k = py_utf8_one(s + *i, n - *i, cp);
    if (!k) { (*i)++; return 0; }
    *i += (size_t)k;
    return 1;
}

int py_is_space_cp(unsigned cp) {
    if (cp == 0x20 || (cp >= 0x09 && cp <= 0x0D) || (cp >= 0x1C && cp <= 0x1F))
        return 1;
    if (cp == 0x85 || cp == 0xA0 || cp == 0x1680) return 1;
    if (cp >= 0x2000 && cp <= 0x200A) return 1;
    return cp == 0x2028 || cp == 0x2029 || cp == 0x202F || cp == 0x205F ||
           cp == 0x3000;
}

int py_utf8_one(const unsigned char *s, size_t n, unsigned *cp) {
    if (!n) return 0;
    unsigned c = s[0];
    if (c < 0x80) { *cp = c; return 1; }
    int need;
    unsigned v;
    if (c >= 0xc2 && c <= 0xdf) { need = 1; v = c & 0x1fu; }
    else if (c >= 0xe0 && c <= 0xef) { need = 2; v = c & 0x0fu; }
    else if (c >= 0xf0 && c <= 0xf4) { need = 3; v = c & 0x07u; }
    else return 0;
    if (n < (size_t)need + 1) return 0;
    for (int k = 1; k <= need; k++) {
        unsigned cc = s[k];
        if ((cc & 0xc0) != 0x80) return 0;
        v = (v << 6) | (cc & 0x3fu);
    }
    if ((need == 2 && v < 0x800) || (need == 3 && v < 0x10000) ||
        (v >= 0xd800 && v <= 0xdfff) || v > 0x10ffff)
        return 0;
    *cp = v;
    return need + 1;
}

/* \w 区间表：由 Python str.isalnum() 生成，733 段。 */
typedef struct { unsigned cp, count; } word_range;
static const word_range g_word[] = {
    {0x30,10}, {0x41,26}, {0x61,26}, {0xAA,1}, {0xB2,2}, {0xB5,1}, {0xB9,2}, {0xBC,3},
    {0xC0,23}, {0xD8,31}, {0xF8,458}, {0x2C6,12}, {0x2E0,5}, {0x2EC,1}, {0x2EE,1}, {0x370,5},
    {0x376,2}, {0x37A,4}, {0x37F,1}, {0x386,1}, {0x388,3}, {0x38C,1}, {0x38E,20}, {0x3A3,83},
    {0x3F7,139}, {0x48A,166}, {0x531,38}, {0x559,1}, {0x560,41}, {0x5D0,27}, {0x5EF,4},
    {0x620,43}, {0x660,10}, {0x66E,2}, {0x671,99}, {0x6D5,1}, {0x6E5,2}, {0x6EE,15}, {0x6FF,1},
    {0x710,1}, {0x712,30}, {0x74D,89}, {0x7B1,1}, {0x7C0,43}, {0x7F4,2}, {0x7FA,1}, {0x800,22},
    {0x81A,1}, {0x824,1}, {0x828,1}, {0x840,25}, {0x860,11}, {0x870,24}, {0x889,6}, {0x8A0,42},
    {0x904,54}, {0x93D,1}, {0x950,1}, {0x958,10}, {0x966,10}, {0x971,16}, {0x985,8}, {0x98F,2},
    {0x993,22}, {0x9AA,7}, {0x9B2,1}, {0x9B6,4}, {0x9BD,1}, {0x9CE,1}, {0x9DC,2}, {0x9DF,3},
    {0x9E6,12}, {0x9F4,6}, {0x9FC,1}, {0xA05,6}, {0xA0F,2}, {0xA13,22}, {0xA2A,7}, {0xA32,2},
    {0xA35,2}, {0xA38,2}, {0xA59,4}, {0xA5E,1}, {0xA66,10}, {0xA72,3}, {0xA85,9}, {0xA8F,3},
    {0xA93,22}, {0xAAA,7}, {0xAB2,2}, {0xAB5,5}, {0xABD,1}, {0xAD0,1}, {0xAE0,2}, {0xAE6,10},
    {0xAF9,1}, {0xB05,8}, {0xB0F,2}, {0xB13,22}, {0xB2A,7}, {0xB32,2}, {0xB35,5}, {0xB3D,1},
    {0xB5C,2}, {0xB5F,3}, {0xB66,10}, {0xB71,7}, {0xB83,1}, {0xB85,6}, {0xB8E,3}, {0xB92,4},
    {0xB99,2}, {0xB9C,1}, {0xB9E,2}, {0xBA3,2}, {0xBA8,3}, {0xBAE,12}, {0xBD0,1}, {0xBE6,13},
    {0xC05,8}, {0xC0E,3}, {0xC12,23}, {0xC2A,16}, {0xC3D,1}, {0xC58,3}, {0xC5D,1}, {0xC60,2},
    {0xC66,10}, {0xC78,7}, {0xC80,1}, {0xC85,8}, {0xC8E,3}, {0xC92,23}, {0xCAA,10}, {0xCB5,5},
    {0xCBD,1}, {0xCDD,2}, {0xCE0,2}, {0xCE6,10}, {0xCF1,2}, {0xD04,9}, {0xD0E,3}, {0xD12,41},
    {0xD3D,1}, {0xD4E,1}, {0xD54,3}, {0xD58,10}, {0xD66,19}, {0xD7A,6}, {0xD85,18}, {0xD9A,24},
    {0xDB3,9}, {0xDBD,1}, {0xDC0,7}, {0xDE6,10}, {0xE01,48}, {0xE32,2}, {0xE40,7}, {0xE50,10},
    {0xE81,2}, {0xE84,1}, {0xE86,5}, {0xE8C,24}, {0xEA5,1}, {0xEA7,10}, {0xEB2,2}, {0xEBD,1},
    {0xEC0,5}, {0xEC6,1}, {0xED0,10}, {0xEDC,4}, {0xF00,1}, {0xF20,20}, {0xF40,8}, {0xF49,36},
    {0xF88,5}, {0x1000,43}, {0x103F,11}, {0x1050,6}, {0x105A,4}, {0x1061,1}, {0x1065,2},
    {0x106E,3}, {0x1075,13}, {0x108E,1}, {0x1090,10}, {0x10A0,38}, {0x10C7,1}, {0x10CD,1},
    {0x10D0,43}, {0x10FC,333}, {0x124A,4}, {0x1250,7}, {0x1258,1}, {0x125A,4}, {0x1260,41},
    {0x128A,4}, {0x1290,33}, {0x12B2,4}, {0x12B8,7}, {0x12C0,1}, {0x12C2,4}, {0x12C8,15},
    {0x12D8,57}, {0x1312,4}, {0x1318,67}, {0x1369,20}, {0x1380,16}, {0x13A0,86}, {0x13F8,6},
    {0x1401,620}, {0x166F,17}, {0x1681,26}, {0x16A0,75}, {0x16EE,11}, {0x1700,18}, {0x171F,19},
    {0x1740,18}, {0x1760,13}, {0x176E,3}, {0x1780,52}, {0x17D7,1}, {0x17DC,1}, {0x17E0,10},
    {0x17F0,10}, {0x1810,10}, {0x1820,89}, {0x1880,5}, {0x1887,34}, {0x18AA,1}, {0x18B0,70},
    {0x1900,31}, {0x1946,40}, {0x1970,5}, {0x1980,44}, {0x19B0,26}, {0x19D0,11}, {0x1A00,23},
    {0x1A20,53}, {0x1A80,10}, {0x1A90,10}, {0x1AA7,1}, {0x1B05,47}, {0x1B45,8}, {0x1B50,10},
    {0x1B83,30}, {0x1BAE,56}, {0x1C00,36}, {0x1C40,10}, {0x1C4D,49}, {0x1C80,9}, {0x1C90,43},
    {0x1CBD,3}, {0x1CE9,4}, {0x1CEE,6}, {0x1CF5,2}, {0x1CFA,1}, {0x1D00,192}, {0x1E00,278},
    {0x1F18,6}, {0x1F20,38}, {0x1F48,6}, {0x1F50,8}, {0x1F59,1}, {0x1F5B,1}, {0x1F5D,1},
    {0x1F5F,31}, {0x1F80,53}, {0x1FB6,7}, {0x1FBE,1}, {0x1FC2,3}, {0x1FC6,7}, {0x1FD0,4},
    {0x1FD6,6}, {0x1FE0,13}, {0x1FF2,3}, {0x1FF6,7}, {0x2070,2}, {0x2074,6}, {0x207F,11},
    {0x2090,13}, {0x2102,1}, {0x2107,1}, {0x210A,10}, {0x2115,1}, {0x2119,5}, {0x2124,1},
    {0x2126,1}, {0x2128,1}, {0x212A,4}, {0x212F,11}, {0x213C,4}, {0x2145,5}, {0x214E,1},
    {0x2150,58}, {0x2460,60}, {0x24EA,22}, {0x2776,30}, {0x2C00,229}, {0x2CEB,4}, {0x2CF2,2},
    {0x2CFD,1}, {0x2D00,38}, {0x2D27,1}, {0x2D2D,1}, {0x2D30,56}, {0x2D6F,1}, {0x2D80,23},
    {0x2DA0,7}, {0x2DA8,7}, {0x2DB0,7}, {0x2DB8,7}, {0x2DC0,7}, {0x2DC8,7}, {0x2DD0,7},
    {0x2DD8,7}, {0x2E2F,1}, {0x3005,3}, {0x3021,9}, {0x3031,5}, {0x3038,5}, {0x3041,86},
    {0x309D,3}, {0x30A1,90}, {0x30FC,4}, {0x3105,43}, {0x3131,94}, {0x3192,4}, {0x31A0,32},
    {0x31F0,16}, {0x3220,10}, {0x3248,8}, {0x3251,15}, {0x3280,10}, {0x32B1,15}, {0x3400,6592},
    {0x4E00,22157}, {0xA4D0,46}, {0xA500,269}, {0xA610,28}, {0xA640,47}, {0xA67F,31},
    {0xA6A0,80}, {0xA717,9}, {0xA722,103}, {0xA78B,64}, {0xA7D0,2}, {0xA7D3,1}, {0xA7D5,5},
    {0xA7F2,16}, {0xA803,3}, {0xA807,4}, {0xA80C,23}, {0xA830,6}, {0xA840,52}, {0xA882,50},
    {0xA8D0,10}, {0xA8F2,6}, {0xA8FB,1}, {0xA8FD,2}, {0xA900,38}, {0xA930,23}, {0xA960,29},
    {0xA984,47}, {0xA9CF,11}, {0xA9E0,5}, {0xA9E6,25}, {0xAA00,41}, {0xAA40,3}, {0xAA44,8},
    {0xAA50,10}, {0xAA60,23}, {0xAA7A,1}, {0xAA7E,50}, {0xAAB1,1}, {0xAAB5,2}, {0xAAB9,5},
    {0xAAC0,1}, {0xAAC2,1}, {0xAADB,3}, {0xAAE0,11}, {0xAAF2,3}, {0xAB01,6}, {0xAB09,6},
    {0xAB11,6}, {0xAB20,7}, {0xAB28,7}, {0xAB30,43}, {0xAB5C,14}, {0xAB70,115}, {0xABF0,10},
    {0xAC00,11172}, {0xD7B0,23}, {0xD7CB,49}, {0xF900,366}, {0xFA70,106}, {0xFB00,7},
    {0xFB13,5}, {0xFB1D,1}, {0xFB1F,10}, {0xFB2A,13}, {0xFB38,5}, {0xFB3E,1}, {0xFB40,2},
    {0xFB43,2}, {0xFB46,108}, {0xFBD3,363}, {0xFD50,64}, {0xFD92,54}, {0xFDF0,12}, {0xFE70,5},
    {0xFE76,135}, {0xFF10,10}, {0xFF21,26}, {0xFF41,26}, {0xFF66,89}, {0xFFC2,6}, {0xFFCA,6},
    {0xFFD2,6}, {0xFFDA,3}, {0x10000,12}, {0x1000D,26}, {0x10028,19}, {0x1003C,2}, {0x1003F,15},
    {0x10050,14}, {0x10080,123}, {0x10107,45}, {0x10140,57}, {0x1018A,2}, {0x10280,29},
    {0x102A0,49}, {0x102E1,27}, {0x10300,36}, {0x1032D,30}, {0x10350,38}, {0x10380,30},
    {0x103A0,36}, {0x103C8,8}, {0x103D1,5}, {0x10400,158}, {0x104A0,10}, {0x104B0,36},
    {0x104D8,36}, {0x10500,40}, {0x10530,52}, {0x10570,11}, {0x1057C,15}, {0x1058C,7},
    {0x10594,2}, {0x10597,11}, {0x105A3,15}, {0x105B3,7}, {0x105BB,2}, {0x10600,311},
    {0x10740,22}, {0x10760,8}, {0x10780,6}, {0x10787,42}, {0x107B2,9}, {0x10800,6}, {0x10808,1},
    {0x1080A,44}, {0x10837,2}, {0x1083C,1}, {0x1083F,23}, {0x10858,31}, {0x10879,38},
    {0x108A7,9}, {0x108E0,19}, {0x108F4,2}, {0x108FB,33}, {0x10920,26}, {0x10980,56},
    {0x109BC,20}, {0x109D2,47}, {0x10A10,4}, {0x10A15,3}, {0x10A19,29}, {0x10A40,9},
    {0x10A60,31}, {0x10A80,32}, {0x10AC0,8}, {0x10AC9,28}, {0x10AEB,5}, {0x10B00,54},
    {0x10B40,22}, {0x10B58,27}, {0x10B78,26}, {0x10BA9,7}, {0x10C00,73}, {0x10C80,51},
    {0x10CC0,51}, {0x10CFA,42}, {0x10D30,10}, {0x10E60,31}, {0x10E80,42}, {0x10EB0,2},
    {0x10F00,40}, {0x10F30,22}, {0x10F51,4}, {0x10F70,18}, {0x10FB0,28}, {0x10FE0,23},
    {0x11003,53}, {0x11052,30}, {0x11071,2}, {0x11075,1}, {0x11083,45}, {0x110D0,25},
    {0x110F0,10}, {0x11103,36}, {0x11136,10}, {0x11144,1}, {0x11147,1}, {0x11150,35},
    {0x11176,1}, {0x11183,48}, {0x111C1,4}, {0x111D0,11}, {0x111DC,1}, {0x111E1,20},
    {0x11200,18}, {0x11213,25}, {0x11280,7}, {0x11288,1}, {0x1128A,4}, {0x1128F,15},
    {0x1129F,10}, {0x112B0,47}, {0x112F0,10}, {0x11305,8}, {0x1130F,2}, {0x11313,22},
    {0x1132A,7}, {0x11332,2}, {0x11335,5}, {0x1133D,1}, {0x11350,1}, {0x1135D,5}, {0x11400,53},
    {0x11447,4}, {0x11450,10}, {0x1145F,3}, {0x11480,48}, {0x114C4,2}, {0x114C7,1},
    {0x114D0,10}, {0x11580,47}, {0x115D8,4}, {0x11600,48}, {0x11644,1}, {0x11650,10},
    {0x11680,43}, {0x116B8,1}, {0x116C0,10}, {0x11700,27}, {0x11730,12}, {0x11740,7},
    {0x11800,44}, {0x118A0,83}, {0x118FF,8}, {0x11909,1}, {0x1190C,8}, {0x11915,2},
    {0x11918,24}, {0x1193F,1}, {0x11941,1}, {0x11950,10}, {0x119A0,8}, {0x119AA,39},
    {0x119E1,1}, {0x119E3,1}, {0x11A00,1}, {0x11A0B,40}, {0x11A3A,1}, {0x11A50,1}, {0x11A5C,46},
    {0x11A9D,1}, {0x11AB0,73}, {0x11C00,9}, {0x11C0A,37}, {0x11C40,1}, {0x11C50,29},
    {0x11C72,30}, {0x11D00,7}, {0x11D08,2}, {0x11D0B,38}, {0x11D46,1}, {0x11D50,10},
    {0x11D60,6}, {0x11D67,2}, {0x11D6A,32}, {0x11D98,1}, {0x11DA0,10}, {0x11EE0,19},
    {0x11FB0,1}, {0x11FC0,21}, {0x12000,922}, {0x12400,111}, {0x12480,196}, {0x12F90,97},
    {0x13000,1071}, {0x14400,583}, {0x16800,569}, {0x16A40,31}, {0x16A60,10}, {0x16A70,79},
    {0x16AC0,10}, {0x16AD0,30}, {0x16B00,48}, {0x16B40,4}, {0x16B50,10}, {0x16B5B,7},
    {0x16B63,21}, {0x16B7D,19}, {0x16E40,87}, {0x16F00,75}, {0x16F50,1}, {0x16F93,13},
    {0x16FE0,2}, {0x16FE3,1}, {0x17000,6136}, {0x18800,1238}, {0x18D00,9}, {0x1AFF0,4},
    {0x1AFF5,7}, {0x1AFFD,2}, {0x1B000,291}, {0x1B150,3}, {0x1B164,4}, {0x1B170,396},
    {0x1BC00,107}, {0x1BC70,13}, {0x1BC80,9}, {0x1BC90,10}, {0x1D2E0,20}, {0x1D360,25},
    {0x1D400,85}, {0x1D456,71}, {0x1D49E,2}, {0x1D4A2,1}, {0x1D4A5,2}, {0x1D4A9,4},
    {0x1D4AE,12}, {0x1D4BB,1}, {0x1D4BD,7}, {0x1D4C5,65}, {0x1D507,4}, {0x1D50D,8}, {0x1D516,7},
    {0x1D51E,28}, {0x1D53B,4}, {0x1D540,5}, {0x1D546,1}, {0x1D54A,7}, {0x1D552,340},
    {0x1D6A8,25}, {0x1D6C2,25}, {0x1D6DC,31}, {0x1D6FC,25}, {0x1D716,31}, {0x1D736,25},
    {0x1D750,31}, {0x1D770,25}, {0x1D78A,31}, {0x1D7AA,25}, {0x1D7C4,8}, {0x1D7CE,50},
    {0x1DF00,31}, {0x1E100,45}, {0x1E137,7}, {0x1E140,10}, {0x1E14E,1}, {0x1E290,30},
    {0x1E2C0,44}, {0x1E2F0,10}, {0x1E7E0,7}, {0x1E7E8,4}, {0x1E7ED,2}, {0x1E7F0,15},
    {0x1E800,197}, {0x1E8C7,9}, {0x1E900,68}, {0x1E94B,1}, {0x1E950,10}, {0x1EC71,59},
    {0x1ECAD,3}, {0x1ECB1,4}, {0x1ED01,45}, {0x1ED2F,15}, {0x1EE00,4}, {0x1EE05,27},
    {0x1EE21,2}, {0x1EE24,1}, {0x1EE27,1}, {0x1EE29,10}, {0x1EE34,4}, {0x1EE39,1}, {0x1EE3B,1},
    {0x1EE42,1}, {0x1EE47,1}, {0x1EE49,1}, {0x1EE4B,1}, {0x1EE4D,3}, {0x1EE51,2}, {0x1EE54,1},
    {0x1EE57,1}, {0x1EE59,1}, {0x1EE5B,1}, {0x1EE5D,1}, {0x1EE5F,1}, {0x1EE61,2}, {0x1EE64,1},
    {0x1EE67,4}, {0x1EE6C,7}, {0x1EE74,4}, {0x1EE79,4}, {0x1EE7E,1}, {0x1EE80,10}, {0x1EE8B,17},
    {0x1EEA1,3}, {0x1EEA5,5}, {0x1EEAB,17}, {0x1F100,13}, {0x1FBF0,10}, {0x20000,42720},
    {0x2A700,4153}, {0x2B740,222}, {0x2B820,5762}, {0x2CEB0,7473}, {0x2F800,542},
    {0x30000,4939},
};

int py_is_word(unsigned cp) {
    if (cp < 0x80)
        return (cp >= '0' && cp <= '9') || (cp >= 'a' && cp <= 'z') ||
               (cp >= 'A' && cp <= 'Z') || cp == '_';
    unsigned lo = 0, hi = sizeof(g_word) / sizeof(g_word[0]);
    while (lo < hi) {
        unsigned mid = lo + (hi - lo) / 2;
        if (cp < g_word[mid].cp) hi = mid;
        else if (cp >= g_word[mid].cp + g_word[mid].count) lo = mid + 1;
        else return 1;
    }
    return 0;
}

char *py_strip_dup_n(const char *s, size_t n, size_t *outn) {
    if (!s) { if (outn) *outn = 0; return xstrdup(""); }
    size_t i = 0, first = 0, last = 0;
    int seen = 0;
    while (i < n) {
        unsigned cp = 0;
        int k = py_utf8_one((const unsigned char *)s + i, n - i, &cp);
        size_t adv = k ? (size_t)k : 1;
        if (!k || !py_is_space_cp(cp)) {
            if (!seen) { first = i; seen = 1; }
            last = i + adv;
        }
        i += adv;
    }
    if (!seen) { if (outn) *outn = 0; return xstrdup(""); }
    if (outn) *outn = last - first;
    return xstrndup(s + first, last - first);
}

char *py_strip_dup(const char *s) {
    return py_strip_dup_n(s, s ? strlen(s) : 0, NULL);
}

char *py_strip(char *s) {
    size_t n = strlen(s);
    size_t a = 0;
    while (a < n) {
        unsigned cp = 0;
        int k = py_utf8_one((const unsigned char *)s + a, n - a, &cp);
        if (!k || !py_is_space_cp(cp)) break;
        a += (size_t)k;
    }
    if (a) memmove(s, s + a, n - a + 1);
    n -= a;
    while (n > 0) {
        /* 从尾部找最后一个非空白码点的结束位置（反向不可靠，正向扫） */
        size_t i = 0, last = 0;
        int seen = 0;
        while (i < n) {
            unsigned cp = 0;
            int k = py_utf8_one((const unsigned char *)s + i, n - i, &cp);
            size_t adv = k ? (size_t)k : 1;
            if (!k || !py_is_space_cp(cp)) { last = i + adv; seen = 1; }
            i += adv;
        }
        if (!seen) { s[0] = 0; return s; }
        n = last;
        break;
    }
    s[n] = 0;
    return s;
}

/* 不可打印区间表：由 Python str.isprintable() 生成，701 段。 */
typedef struct { unsigned cp, count; } np_range;
static const np_range g_nonprint[] = {
    {0x0,32}, {0x7F,34}, {0xAD,1}, {0x378,2}, {0x380,4}, {0x38B,1}, {0x38D,1}, {0x3A2,1},
    {0x530,1}, {0x557,2}, {0x58B,2}, {0x590,1}, {0x5C8,8}, {0x5EB,4}, {0x5F5,17}, {0x61C,1},
    {0x6DD,1}, {0x70E,2}, {0x74B,2}, {0x7B2,14}, {0x7FB,2}, {0x82E,2}, {0x83F,1}, {0x85C,2},
    {0x85F,1}, {0x86B,5}, {0x88F,9}, {0x8E2,1}, {0x984,1}, {0x98D,2}, {0x991,2}, {0x9A9,1},
    {0x9B1,1}, {0x9B3,3}, {0x9BA,2}, {0x9C5,2}, {0x9C9,2}, {0x9CF,8}, {0x9D8,4}, {0x9DE,1},
    {0x9E4,2}, {0x9FF,2}, {0xA04,1}, {0xA0B,4}, {0xA11,2}, {0xA29,1}, {0xA31,1}, {0xA34,1},
    {0xA37,1}, {0xA3A,2}, {0xA3D,1}, {0xA43,4}, {0xA49,2}, {0xA4E,3}, {0xA52,7}, {0xA5D,1},
    {0xA5F,7}, {0xA77,10}, {0xA84,1}, {0xA8E,1}, {0xA92,1}, {0xAA9,1}, {0xAB1,1}, {0xAB4,1},
    {0xABA,2}, {0xAC6,1}, {0xACA,1}, {0xACE,2}, {0xAD1,15}, {0xAE4,2}, {0xAF2,7}, {0xB00,1},
    {0xB04,1}, {0xB0D,2}, {0xB11,2}, {0xB29,1}, {0xB31,1}, {0xB34,1}, {0xB3A,2}, {0xB45,2},
    {0xB49,2}, {0xB4E,7}, {0xB58,4}, {0xB5E,1}, {0xB64,2}, {0xB78,10}, {0xB84,1}, {0xB8B,3},
    {0xB91,1}, {0xB96,3}, {0xB9B,1}, {0xB9D,1}, {0xBA0,3}, {0xBA5,3}, {0xBAB,3}, {0xBBA,4},
    {0xBC3,3}, {0xBC9,1}, {0xBCE,2}, {0xBD1,6}, {0xBD8,14}, {0xBFB,5}, {0xC0D,1}, {0xC11,1},
    {0xC29,1}, {0xC3A,2}, {0xC45,1}, {0xC49,1}, {0xC4E,7}, {0xC57,1}, {0xC5B,2}, {0xC5E,2},
    {0xC64,2}, {0xC70,7}, {0xC8D,1}, {0xC91,1}, {0xCA9,1}, {0xCB4,1}, {0xCBA,2}, {0xCC5,1},
    {0xCC9,1}, {0xCCE,7}, {0xCD7,6}, {0xCDF,1}, {0xCE4,2}, {0xCF0,1}, {0xCF3,13}, {0xD0D,1},
    {0xD11,1}, {0xD45,1}, {0xD49,1}, {0xD50,4}, {0xD64,2}, {0xD80,1}, {0xD84,1}, {0xD97,3},
    {0xDB2,1}, {0xDBC,1}, {0xDBE,2}, {0xDC7,3}, {0xDCB,4}, {0xDD5,1}, {0xDD7,1}, {0xDE0,6},
    {0xDF0,2}, {0xDF5,12}, {0xE3B,4}, {0xE5C,37}, {0xE83,1}, {0xE85,1}, {0xE8B,1}, {0xEA4,1},
    {0xEA6,1}, {0xEBE,2}, {0xEC5,1}, {0xEC7,1}, {0xECE,2}, {0xEDA,2}, {0xEE0,32}, {0xF48,1},
    {0xF6D,4}, {0xF98,1}, {0xFBD,1}, {0xFCD,1}, {0xFDB,37}, {0x10C6,1}, {0x10C8,5}, {0x10CE,2},
    {0x1249,1}, {0x124E,2}, {0x1257,1}, {0x1259,1}, {0x125E,2}, {0x1289,1}, {0x128E,2},
    {0x12B1,1}, {0x12B6,2}, {0x12BF,1}, {0x12C1,1}, {0x12C6,2}, {0x12D7,1}, {0x1311,1},
    {0x1316,2}, {0x135B,2}, {0x137D,3}, {0x139A,6}, {0x13F6,2}, {0x13FE,2}, {0x1680,1},
    {0x169D,3}, {0x16F9,7}, {0x1716,9}, {0x1737,9}, {0x1754,12}, {0x176D,1}, {0x1771,1},
    {0x1774,12}, {0x17DE,2}, {0x17EA,6}, {0x17FA,6}, {0x180E,1}, {0x181A,6}, {0x1879,7},
    {0x18AB,5}, {0x18F6,10}, {0x191F,1}, {0x192C,4}, {0x193C,4}, {0x1941,3}, {0x196E,2},
    {0x1975,11}, {0x19AC,4}, {0x19CA,6}, {0x19DB,3}, {0x1A1C,2}, {0x1A5F,1}, {0x1A7D,2},
    {0x1A8A,6}, {0x1A9A,6}, {0x1AAE,2}, {0x1ACF,49}, {0x1B4D,3}, {0x1B7F,1}, {0x1BF4,8},
    {0x1C38,3}, {0x1C4A,3}, {0x1C89,7}, {0x1CBB,2}, {0x1CC8,8}, {0x1CFB,5}, {0x1F16,2},
    {0x1F1E,2}, {0x1F46,2}, {0x1F4E,2}, {0x1F58,1}, {0x1F5A,1}, {0x1F5C,1}, {0x1F5E,1},
    {0x1F7E,2}, {0x1FB5,1}, {0x1FC5,1}, {0x1FD4,2}, {0x1FDC,1}, {0x1FF0,2}, {0x1FF5,1},
    {0x1FFF,17}, {0x2028,8}, {0x205F,17}, {0x2072,2}, {0x208F,1}, {0x209D,3}, {0x20C1,15},
    {0x20F1,15}, {0x218C,4}, {0x2427,25}, {0x244B,21}, {0x2B74,2}, {0x2B96,1}, {0x2CF4,5},
    {0x2D26,1}, {0x2D28,5}, {0x2D2E,2}, {0x2D68,7}, {0x2D71,14}, {0x2D97,9}, {0x2DA7,1},
    {0x2DAF,1}, {0x2DB7,1}, {0x2DBF,1}, {0x2DC7,1}, {0x2DCF,1}, {0x2DD7,1}, {0x2DDF,1},
    {0x2E5E,34}, {0x2E9A,1}, {0x2EF4,12}, {0x2FD6,26}, {0x2FFC,5}, {0x3040,1}, {0x3097,2},
    {0x3100,5}, {0x3130,1}, {0x318F,1}, {0x31E4,12}, {0x321F,1}, {0xA48D,3}, {0xA4C7,9},
    {0xA62C,20}, {0xA6F8,8}, {0xA7CB,5}, {0xA7D2,1}, {0xA7D4,1}, {0xA7DA,24}, {0xA82D,3},
    {0xA83A,6}, {0xA878,8}, {0xA8C6,8}, {0xA8DA,6}, {0xA954,11}, {0xA97D,3}, {0xA9CE,1},
    {0xA9DA,4}, {0xA9FF,1}, {0xAA37,9}, {0xAA4E,2}, {0xAA5A,2}, {0xAAC3,24}, {0xAAF7,10},
    {0xAB07,2}, {0xAB0F,2}, {0xAB17,9}, {0xAB27,1}, {0xAB2F,1}, {0xAB6C,4}, {0xABEE,2},
    {0xABFA,6}, {0xD7A4,12}, {0xD7C7,4}, {0xD7FC,8452}, {0xFA6E,2}, {0xFADA,38}, {0xFB07,12},
    {0xFB18,5}, {0xFB37,1}, {0xFB3D,1}, {0xFB3F,1}, {0xFB42,1}, {0xFB45,1}, {0xFBC3,16},
    {0xFD90,2}, {0xFDC8,7}, {0xFDD0,32}, {0xFE1A,6}, {0xFE53,1}, {0xFE67,1}, {0xFE6C,4},
    {0xFE75,1}, {0xFEFD,4}, {0xFFBF,3}, {0xFFC8,2}, {0xFFD0,2}, {0xFFD8,2}, {0xFFDD,3},
    {0xFFE7,1}, {0xFFEF,13}, {0xFFFE,2}, {0x1000C,1}, {0x10027,1}, {0x1003B,1}, {0x1003E,1},
    {0x1004E,2}, {0x1005E,34}, {0x100FB,5}, {0x10103,4}, {0x10134,3}, {0x1018F,1}, {0x1019D,3},
    {0x101A1,47}, {0x101FE,130}, {0x1029D,3}, {0x102D1,15}, {0x102FC,4}, {0x10324,9},
    {0x1034B,5}, {0x1037B,5}, {0x1039E,1}, {0x103C4,4}, {0x103D6,42}, {0x1049E,2}, {0x104AA,6},
    {0x104D4,4}, {0x104FC,4}, {0x10528,8}, {0x10564,11}, {0x1057B,1}, {0x1058B,1}, {0x10593,1},
    {0x10596,1}, {0x105A2,1}, {0x105B2,1}, {0x105BA,1}, {0x105BD,67}, {0x10737,9}, {0x10756,10},
    {0x10768,24}, {0x10786,1}, {0x107B1,1}, {0x107BB,69}, {0x10806,2}, {0x10809,1}, {0x10836,1},
    {0x10839,3}, {0x1083D,2}, {0x10856,1}, {0x1089F,8}, {0x108B0,48}, {0x108F3,1}, {0x108F6,5},
    {0x1091C,3}, {0x1093A,5}, {0x10940,64}, {0x109B8,4}, {0x109D0,2}, {0x10A04,1}, {0x10A07,5},
    {0x10A14,1}, {0x10A18,1}, {0x10A36,2}, {0x10A3B,4}, {0x10A49,7}, {0x10A59,7}, {0x10AA0,32},
    {0x10AE7,4}, {0x10AF7,9}, {0x10B36,3}, {0x10B56,2}, {0x10B73,5}, {0x10B92,7}, {0x10B9D,12},
    {0x10BB0,80}, {0x10C49,55}, {0x10CB3,13}, {0x10CF3,7}, {0x10D28,8}, {0x10D3A,294},
    {0x10E7F,1}, {0x10EAA,1}, {0x10EAE,2}, {0x10EB2,78}, {0x10F28,8}, {0x10F5A,22},
    {0x10F8A,38}, {0x10FCC,20}, {0x10FF7,9}, {0x1104E,4}, {0x11076,9}, {0x110BD,1},
    {0x110C3,13}, {0x110E9,7}, {0x110FA,6}, {0x11135,1}, {0x11148,8}, {0x11177,9}, {0x111E0,1},
    {0x111F5,11}, {0x11212,1}, {0x1123F,65}, {0x11287,1}, {0x11289,1}, {0x1128E,1}, {0x1129E,1},
    {0x112AA,6}, {0x112EB,5}, {0x112FA,6}, {0x11304,1}, {0x1130D,2}, {0x11311,2}, {0x11329,1},
    {0x11331,1}, {0x11334,1}, {0x1133A,1}, {0x11345,2}, {0x11349,2}, {0x1134E,2}, {0x11351,6},
    {0x11358,5}, {0x11364,2}, {0x1136D,3}, {0x11375,139}, {0x1145C,1}, {0x11462,30},
    {0x114C8,8}, {0x114DA,166}, {0x115B6,2}, {0x115DE,34}, {0x11645,11}, {0x1165A,6},
    {0x1166D,19}, {0x116BA,6}, {0x116CA,54}, {0x1171B,2}, {0x1172C,4}, {0x11747,185},
    {0x1183C,100}, {0x118F3,12}, {0x11907,2}, {0x1190A,2}, {0x11914,1}, {0x11917,1},
    {0x11936,1}, {0x11939,2}, {0x11947,9}, {0x1195A,70}, {0x119A8,2}, {0x119D8,2}, {0x119E5,27},
    {0x11A48,8}, {0x11AA3,13}, {0x11AF9,263}, {0x11C09,1}, {0x11C37,1}, {0x11C46,10},
    {0x11C6D,3}, {0x11C90,2}, {0x11CA8,1}, {0x11CB7,73}, {0x11D07,1}, {0x11D0A,1}, {0x11D37,3},
    {0x11D3B,1}, {0x11D3E,1}, {0x11D48,8}, {0x11D5A,6}, {0x11D66,1}, {0x11D69,1}, {0x11D8F,1},
    {0x11D92,1}, {0x11D99,7}, {0x11DAA,310}, {0x11EF9,183}, {0x11FB1,15}, {0x11FF2,13},
    {0x1239A,102}, {0x1246F,1}, {0x12475,11}, {0x12544,2636}, {0x12FF3,13}, {0x1342F,4049},
    {0x14647,8633}, {0x16A39,7}, {0x16A5F,1}, {0x16A6A,4}, {0x16ABF,1}, {0x16ACA,6},
    {0x16AEE,2}, {0x16AF6,10}, {0x16B46,10}, {0x16B5A,1}, {0x16B62,1}, {0x16B78,5},
    {0x16B90,688}, {0x16E9B,101}, {0x16F4B,4}, {0x16F88,7}, {0x16FA0,64}, {0x16FE5,11},
    {0x16FF2,14}, {0x187F8,8}, {0x18CD6,42}, {0x18D09,8935}, {0x1AFF4,1}, {0x1AFFC,1},
    {0x1AFFF,1}, {0x1B123,45}, {0x1B153,17}, {0x1B168,8}, {0x1B2FC,2308}, {0x1BC6B,5},
    {0x1BC7D,3}, {0x1BC89,7}, {0x1BC9A,2}, {0x1BCA0,4704}, {0x1CF2E,2}, {0x1CF47,9},
    {0x1CFC4,60}, {0x1D0F6,10}, {0x1D127,2}, {0x1D173,8}, {0x1D1EB,21}, {0x1D246,154},
    {0x1D2F4,12}, {0x1D357,9}, {0x1D379,135}, {0x1D455,1}, {0x1D49D,1}, {0x1D4A0,2},
    {0x1D4A3,2}, {0x1D4A7,2}, {0x1D4AD,1}, {0x1D4BA,1}, {0x1D4BC,1}, {0x1D4C4,1}, {0x1D506,1},
    {0x1D50B,2}, {0x1D515,1}, {0x1D51D,1}, {0x1D53A,1}, {0x1D53F,1}, {0x1D545,1}, {0x1D547,3},
    {0x1D551,1}, {0x1D6A6,2}, {0x1D7CC,2}, {0x1DA8C,15}, {0x1DAA0,1}, {0x1DAB0,1104},
    {0x1DF1F,225}, {0x1E007,1}, {0x1E019,2}, {0x1E022,1}, {0x1E025,1}, {0x1E02B,213},
    {0x1E12D,3}, {0x1E13E,2}, {0x1E14A,4}, {0x1E150,320}, {0x1E2AF,17}, {0x1E2FA,5},
    {0x1E300,1248}, {0x1E7E7,1}, {0x1E7EC,1}, {0x1E7EF,1}, {0x1E7FF,1}, {0x1E8C5,2},
    {0x1E8D7,41}, {0x1E94C,4}, {0x1E95A,4}, {0x1E960,785}, {0x1ECB5,76}, {0x1ED3E,194},
    {0x1EE04,1}, {0x1EE20,1}, {0x1EE23,1}, {0x1EE25,2}, {0x1EE28,1}, {0x1EE33,1}, {0x1EE38,1},
    {0x1EE3A,1}, {0x1EE3C,6}, {0x1EE43,4}, {0x1EE48,1}, {0x1EE4A,1}, {0x1EE4C,1}, {0x1EE50,1},
    {0x1EE53,1}, {0x1EE55,2}, {0x1EE58,1}, {0x1EE5A,1}, {0x1EE5C,1}, {0x1EE5E,1}, {0x1EE60,1},
    {0x1EE63,1}, {0x1EE65,2}, {0x1EE6B,1}, {0x1EE73,1}, {0x1EE78,1}, {0x1EE7D,1}, {0x1EE7F,1},
    {0x1EE8A,1}, {0x1EE9C,5}, {0x1EEA4,1}, {0x1EEAA,1}, {0x1EEBC,52}, {0x1EEF2,270},
    {0x1F02C,4}, {0x1F094,12}, {0x1F0AF,2}, {0x1F0C0,1}, {0x1F0D0,1}, {0x1F0F6,10},
    {0x1F1AE,56}, {0x1F203,13}, {0x1F23C,4}, {0x1F249,7}, {0x1F252,14}, {0x1F266,154},
    {0x1F6D8,5}, {0x1F6ED,3}, {0x1F6FD,3}, {0x1F774,12}, {0x1F7D9,7}, {0x1F7EC,4}, {0x1F7F1,15},
    {0x1F80C,4}, {0x1F848,8}, {0x1F85A,6}, {0x1F888,8}, {0x1F8AE,2}, {0x1F8B2,78}, {0x1FA54,12},
    {0x1FA6E,2}, {0x1FA75,3}, {0x1FA7D,3}, {0x1FA87,9}, {0x1FAAD,3}, {0x1FABB,5}, {0x1FAC6,10},
    {0x1FADA,6}, {0x1FAE8,8}, {0x1FAF7,9}, {0x1FB93,1}, {0x1FBCB,37}, {0x1FBFA,1030},
    {0x2A6E0,32}, {0x2B739,7}, {0x2B81E,2}, {0x2CEA2,14}, {0x2EBE1,3103}, {0x2FA1E,1506},
    {0x3134B,716213}, {0xE01F0,196112},
};

int py_is_printable(unsigned cp) {
    unsigned lo = 0, hi = sizeof(g_nonprint) / sizeof(g_nonprint[0]);
    while (lo < hi) {
        unsigned mid = lo + (hi - lo) / 2;
        if (cp < g_nonprint[mid].cp) hi = mid;
        else if (cp >= g_nonprint[mid].cp + g_nonprint[mid].count) lo = mid + 1;
        else return 0;
    }
    return 1;
}

void py_repr_str(buf *out, const char *s, size_t n) {
    char quote = '\'';
    if (memchr(s, '\'', n) && !memchr(s, '"', n)) quote = '"';
    buf_putc(out, quote);
    size_t i = 0;
    while (i < n) {
        unsigned cp = 0;
        int k = py_utf8_one((const unsigned char *)s + i, n - i, &cp);
        if (!k) {                                  /* 非法字节：按单字节转义 */
            buf_printf(out, "\\x%02x", (unsigned char)s[i]);
            i++;
            continue;
        }
        i += (size_t)k;
        if (cp == (unsigned)quote || cp == '\\') {
            buf_putc(out, '\\');
            buf_putc(out, (char)cp);
        } else if (cp == '\n') buf_puts(out, "\\n");
        else if (cp == '\r') buf_puts(out, "\\r");
        else if (cp == '\t') buf_puts(out, "\\t");
        else if (!py_is_printable(cp)) {
            if (cp <= 0xff) buf_printf(out, "\\x%02x", cp);
            else if (cp <= 0xffff) buf_printf(out, "\\u%04x", cp);
            else buf_printf(out, "\\U%08x", cp);
        } else {
            buf_append(out, s + i - (size_t)k, (size_t)k);
        }
    }
    buf_putc(out, quote);
}

char *py_repr_n(const char *s, size_t n) {
    buf b;
    buf_init(&b);
    py_repr_str(&b, s, n);
    char *r = xstrdup(buf_cstr(&b));
    buf_free(&b);
    return r;
}

int py_has_space_or_ctrl(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned cp = 0;
        int k = py_utf8_one((const unsigned char *)s + i, n - i, &cp);
        if (!k) {
            if ((unsigned char)s[i] < 0x20) return 1;
            i++;
            continue;
        }
        if (cp < 0x20 || py_is_space_cp(cp)) return 1;
        i += (size_t)k;
    }
    return 0;
}

/* 解一个 UTF-8 码点；成功返回字节数（1..4），非法返回 0。 */
int py_int128_parse(const char *str, __int128 *out) {
    if (!str) return 0;
    const unsigned char *s = (const unsigned char *)str;
    size_t n = strlen(str);

    size_t a = 0;
    while (a < n) {
        unsigned cp = 0;
        int k = py_utf8_one(s + a, n - a, &cp);
        if (!k || !py_is_space_cp(cp)) break;
        a += (size_t)k;
    }
    size_t b = a, last = a;
    while (b < n) {
        unsigned cp = 0;
        int k = py_utf8_one(s + b, n - b, &cp);
        if (!k) { last = n; break; }        /* 非法字节交给数字解析判失败 */
        if (!py_is_space_cp(cp)) last = b + (size_t)k;
        b += (size_t)k;
    }
    if (a >= last) return 0;

    const unsigned char *p = s + a;
    const unsigned char *e = s + last;
    int neg = 0;
    if (*p == '+' || *p == '-') { neg = (*p == '-'); p++; }
    if (p >= e) return 0;
    __int128 v = 0;
    const __int128 lim = ((__int128)1) << 126;
    int prev_digit = 0, overflow = 0;
    while (p < e) {
        if (*p == '_') {
            if (!prev_digit) return 0;
            prev_digit = 0;
            p++;
            continue;
        }
        int d = -1;
        if (*p >= '0' && *p <= '9') {
            d = *p - '0';
            p++;
        } else {
            unsigned cp = 0;
            int k = py_utf8_one(p, (size_t)(e - p), &cp);
            if (!k || !uni_decimal(cp, &d)) return 0;
            p += k;
        }
        if (v > (lim - d) / 10) overflow = 1;
        else v = v * 10 + d;
        prev_digit = 1;
    }
    if (!prev_digit) return 0;
    if (overflow) v = lim;
    *out = neg ? -v : v;
    return 1;
}

char *py_int_canon_n(const char *str, size_t n) {
    if (!str) return NULL;
    const unsigned char *s = (const unsigned char *)str;
    size_t a = 0;
    while (a < n) {
        unsigned cp = 0;
        int k = py_utf8_one(s + a, n - a, &cp);
        if (!k || !py_is_space_cp(cp)) break;
        a += (size_t)k;
    }
    size_t b = a, last = a;
    while (b < n) {
        unsigned cp = 0;
        int k = py_utf8_one(s + b, n - b, &cp);
        if (!k) { last = n; break; }
        if (!py_is_space_cp(cp)) last = b + (size_t)k;
        b += (size_t)k;
    }
    if (a >= last) return NULL;

    const unsigned char *p = s + a;
    const unsigned char *e = s + last;
    int neg = 0;
    if (*p == '+' || *p == '-') { neg = (*p == '-'); p++; }
    if (p >= e) return NULL;

    buf d;
    buf_init(&d);
    int prev_digit = 0;
    while (p < e) {
        if (*p == '_') {
            if (!prev_digit) { buf_free(&d); return NULL; }
            prev_digit = 0;
            p++;
            continue;
        }
        int v = -1;
        if (*p >= '0' && *p <= '9') { v = *p - '0'; p++; }
        else {
            unsigned cp = 0;
            int k = py_utf8_one(p, (size_t)(e - p), &cp);
            if (!k || !uni_decimal(cp, &v)) { buf_free(&d); return NULL; }
            p += k;
        }
        buf_putc(&d, (char)('0' + v));
        prev_digit = 1;
    }
    if (!prev_digit) { buf_free(&d); return NULL; }
    buf_cstr(&d);
    size_t i = 0;
    while (i + 1 < d.n && d.p[i] == '0') i++;
    buf o;
    buf_init(&o);
    if (neg && !(d.n - i == 1 && d.p[i] == '0')) buf_putc(&o, '-');
    buf_append(&o, d.p + i, d.n - i);
    char *r = xstrdup(buf_cstr(&o));
    buf_free(&o);
    buf_free(&d);
    return r;
}

char *py_int_canon(const char *s) {
    return py_int_canon_n(s, s ? strlen(s) : 0);
}

int py_int_parse(const char *s, long long *out) {
    __int128 v;
    if (!py_int128_parse(s, &v)) return 0;
    if (v > (__int128)LLONG_MAX) v = (__int128)LLONG_MAX;
    if (v < (__int128)LLONG_MIN) v = (__int128)LLONG_MIN;
    *out = (long long)v;
    return 1;
}

int q_int(const char *query, const char *key, long long dflt, long long *out) {
    char tmp[64];
    int got = q_get(query, key, tmp, sizeof(tmp));
    if (got == 0) { *out = dflt; return 0; }
    if (got < 0) return -1;
    long long v = 0;
    if (!py_int_parse(tmp, &v)) return -1;
    *out = v;
    return 1;
}

int q_bool(const char *query, const char *key, int dflt) {
    char tmp[16];
    int got = q_get(query, key, tmp, sizeof(tmp));
    if (got != 1) return dflt;
    return (!strcmp(tmp, "1") || !strcmp(tmp, "true") || !strcmp(tmp, "yes")) ? 1 : 0;
}
