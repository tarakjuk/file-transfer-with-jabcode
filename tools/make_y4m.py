# 송신 프레임 PNG들 -> 카메라처럼 왜곡된 y4m (Chromium 가짜 카메라 입력용)
import sys, glob, io, random
import numpy as np
from PIL import Image
from degrade import degrade
src, out = sys.argv[1], sys.argv[2]
rep = int(sys.argv[3]) if len(sys.argv) > 3 else 5   # 코드 프레임 하나당 비디오 프레임 수 (30fps/6fps)
kw = eval('dict(%s)' % (sys.argv[4] if len(sys.argv) > 4 else ''))
W, H = 1280, 720
rnd = random.Random(3); np.random.seed(3)
with open(out, 'wb') as f:
    f.write(f'YUV4MPEG2 W{W} H{H} F30:1 Ip A1:1 C420jpeg\n'.encode())
    for p in sorted(glob.glob(src + '/*.png')):
        jpg = degrade(Image.open(p), W, H, rnd=rnd, **kw)
        ycc = Image.open(io.BytesIO(jpg)).convert('YCbCr')
        y, cb, cr = ycc.split()
        cb = cb.resize((W // 2, H // 2), Image.BILINEAR); cr = cr.resize((W // 2, H // 2), Image.BILINEAR)
        fr = b'FRAME\n' + y.tobytes() + cb.tobytes() + cr.tobytes()
        for _ in range(rep):
            f.write(fr)
