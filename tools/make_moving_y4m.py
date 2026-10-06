"""1080p 가짜 카메라 영상(y4m): 화면 높이의 FRAC 크기 JAB Code 가 천천히 움직이며 송신 fps 로 바뀐다.
  python make_moving_y4m.py out.y4m 보낼파일 코드크기 [표시프레임수=60] [병렬심볼=1|4] [송신fps=6] [카메라fps=30]
    코드크기 = 코드 영역(여백 제외) 한 변 / 화면 높이 (예 0.28). 병렬심볼 4 = JAB 다중 심볼 2×2 (송신기 기본값과 동일)
  환경변수 JAB_VER / JAB_ECC 로 심볼 설정 변경 (기본 버전 8 / ECC 5, 색 수는 송신기와 같이 8색 고정)
  (Windows: sender 의 libjabcode.dll 사용. 1080p 4:2:0 한 장 ≈ 3.1MB)
"""
import math, os, random, sys, zlib
import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sender"))
from jabcode_backend import COLOR_NUMBER as COLORS, MULTI_2X2, SINGLE, load_backend, probe_capacity
from jabfountain import HEADER_SIZE, LAYOUT_MULTI2, LAYOUT_SINGLE, FountainEncoder, build_container, packet_flags

OUT, SENT, FRAC = sys.argv[1], sys.argv[2], float(sys.argv[3])
NC = int(sys.argv[4]) if len(sys.argv) > 4 else 60
GRID = 4 if (len(sys.argv) > 5 and sys.argv[5] == "4") else 1
POS = MULTI_2X2 if GRID == 4 else SINGLE
FLAGS = packet_flags(LAYOUT_MULTI2 if GRID == 4 else LAYOUT_SINGLE)
SFPS = float(sys.argv[6]) if len(sys.argv) > 6 else 6
W, H = 1920, 1080
FPS = int(sys.argv[7]) if len(sys.argv) > 7 else 30
REP = max(1, round(FPS / SFPS))
VER, ECC = (int(os.environ.get(k, d)) for k, d in (("JAB_VER", 8), ("JAB_ECC", 5)))
QZ, M = 4, 8

be = load_backend()
cap = probe_capacity(be, COLORS, VER, ECC, POS)
block = ((cap - HEADER_SIZE) // 4) * 4
data = open(SENT, "rb").read()
cont = build_container(SENT, data, True)
sess = (zlib.crc32(cont) ^ (zlib.crc32(f"{COLORS}/{VER}/{ECC}/{block}{'/m4' if GRID == 4 else ''}".encode()) << 1)) & 0xFFFFFFFF or 1
enc = FountainEncoder(cont, block, sess)
print(f"K={enc.k} block={block} session={sess:08X} grid={GRID} 송신 {SFPS}fps (비디오 {REP}장/프레임)", file=sys.stderr)

rnd = random.Random(5); np.random.seed(5)
codes = []
for f0 in range(NC):
    im = be.generate(enc.packet(1000 + f0, FLAGS).data, COLORS, VER, ECC, POS)   # 다중 심볼이면 4칸이 한 장
    U = im.width                                       # 코드 영역 한 변 (모듈)
    pad = Image.new("RGB", ((U + 2 * QZ) * M, (U + 2 * QZ) * M), "white")
    pad.paste(Image.frombytes("RGBA", (U, U), im.rgba).convert("RGB").resize((U * M, U * M), Image.NEAREST), (QZ * M, QZ * M))
    a = np.asarray(pad).astype(np.float32)          # 모니터 대비/화이트밸런스
    a = (28 + (a / 255.0) ** 1.05 * (225 - 28)) * np.array([0.97, 1.0, 1.04])
    codes.append(Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)))


def solve(src, dst):
    A, B = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); B.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); B.append(v)
    return np.linalg.solve(np.array(A, float), np.array(B, float)).tolist()


total = NC * REP
with open(OUT, "wb") as f:
    f.write(f"YUV4MPEG2 W{W} H{H} F{FPS}:1 Ip A1:1 C420jpeg\n".encode())
    for i in range(total):
        t = i / FPS
        T = total / FPS
        side = H * FRAC * (U + 2 * QZ) / U
        amp = max(0.0, 1 - side / H)                   # 큰 코드는 화면 밖으로 나가지 않게 이동 폭 축소
        cx = W / 2 + 0.28 * W * amp * math.sin(2 * math.pi * t / T)
        cy = H / 2 + 0.18 * H * amp * math.sin(2 * math.pi * 2 * t / T)
        pad = codes[i // REP]
        s = side / 2
        k = 0.04 * side
        dst = [(cx - s + k, cy - s), (cx + s, cy - s + k), (cx + s - k, cy + s), (cx - s, cy + s - k)]
        pw = pad.width
        c = solve([(0, 0), (pw, 0), (pw, pw), (0, pw)], dst)
        cam = pad.transform((W, H), Image.PERSPECTIVE, c, Image.BILINEAR, fillcolor=(38, 38, 44))
        cam = cam.filter(ImageFilter.GaussianBlur(0.7))
        b = np.asarray(cam).astype(np.float32) + np.random.normal(0, 4, (H, W, 3))
        cam = Image.fromarray(np.clip(b, 0, 255).astype(np.uint8)).convert("YCbCr")
        y, cb, cr = cam.split()
        f.write(b"FRAME\n" + y.tobytes() + cb.resize((W // 2, H // 2), Image.BILINEAR).tobytes()
                + cr.resize((W // 2, H // 2), Image.BILINEAR).tobytes())
        if i % 50 == 0:
            print("frame", i, file=sys.stderr)
print("done", OUT, file=sys.stderr)
