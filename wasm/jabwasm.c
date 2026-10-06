/* libjabcode -> WebAssembly 디코더 래퍼 (wasi-sdk reactor 빌드) */
#include <stdlib.h>
#include <string.h>
#include "jabcode.h"

static jab_bitmap* g_bmp = NULL;
static int g_cap = 0;
static jab_data* g_out = NULL;
static int g_status = 0;
static jab_decoded_symbol g_syms[MAX_SYMBOL_NUMBER];

/* w*h RGBA 버퍼 포인터를 돌려준다. JS가 여기에 픽셀을 써 넣는다. */
__attribute__((export_name("jw_buffer")))
unsigned char* jw_buffer(int w, int h) {
    int need = w * h * 4;
    if (!g_bmp || need > g_cap) {
        free(g_bmp);
        g_bmp = (jab_bitmap*)malloc(sizeof(jab_bitmap) + need);
        if (!g_bmp) { g_cap = 0; return NULL; }
        g_cap = need;
    }
    g_bmp->width = w; g_bmp->height = h;
    g_bmp->bits_per_pixel = 32; g_bmp->bits_per_channel = 8; g_bmp->channel_count = 4;
    return g_bmp->pixel;
}

/* 디코드. 성공 시 데이터 길이(>=0), 실패 시 -1 - status */
__attribute__((export_name("jw_decode")))
int jw_decode(int mode) {
    if (g_out) { free(g_out); g_out = NULL; }
    if (!g_bmp) return -1;
    g_status = 0;
    g_out = decodeJABCodeEx(g_bmp, mode, &g_status, g_syms, MAX_SYMBOL_NUMBER);
    if (!g_out) return -1 - g_status;
    return g_out->length;
}

__attribute__((export_name("jw_result")))
char* jw_result(void) { return g_out ? g_out->data : NULL; }

__attribute__((export_name("jw_status")))
int jw_status(void) { return g_status; }

/* 마스터 심볼 위치 정보 (디코드 실패해도 파인더 패턴 4개를 찾았으면 채워짐)
 * out[0..1]=side_size(x,y) out[2]=module_size out[3..10]=파인더 패턴 중심 4개 (x,y)
 * 반환: 1 = 위치 있음, 0 = 미검출 */
__attribute__((export_name("jw_geom")))
int jw_geom(float* out) {
    jab_decoded_symbol* s = &g_syms[0];
    if (!(s->module_size > 0) || s->side_size.x <= 0) return 0;
    out[0] = (float)s->side_size.x; out[1] = (float)s->side_size.y; out[2] = s->module_size;
    for (int i = 0; i < 4; i++) { out[3 + 2*i] = s->pattern_positions[i].x; out[4 + 2*i] = s->pattern_positions[i].y; }
    return 1;
}
