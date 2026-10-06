"""
디버그/오프라인용: 송신 프레임을 PNG 파일로 저장한다.
  python export_frames.py 파일 출력폴더 [--count N] [--version 8] [--ecc 5] [--scale 8]   (색 수는 8색 고정)
"""
import argparse
import os
import random
import sys
import zlib

from jabcode_backend import COLOR_NUMBER, load_backend, probe_capacity
from jabfountain import HEADER_SIZE, FountainEncoder, build_container


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("outdir")
    ap.add_argument("--count", type=int, default=0, help="0 이면 K*1.2+10")
    ap.add_argument("--version", type=int, default=8)
    ap.add_argument("--ecc", type=int, default=5)
    ap.add_argument("--scale", type=int, default=8)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--prefer", default=None)
    a = ap.parse_args()
    from PIL import Image, ImageOps

    b = load_backend(a.prefer)
    cap = probe_capacity(b, COLOR_NUMBER, a.version, a.ecc)
    block = ((cap - HEADER_SIZE) // 4) * 4
    data = open(a.file, "rb").read()
    cont = build_container(a.file, data)
    session = (zlib.crc32(cont) ^ (zlib.crc32(f"{COLOR_NUMBER}/{a.version}/{a.ecc}/{block}".encode()) << 1)) & 0xFFFFFFFF or 1
    enc = FountainEncoder(cont, block, session)
    n = a.count or int(enc.k * 1.2) + 10
    os.makedirs(a.outdir, exist_ok=True)
    for i in range(n):
        seed = a.start + i
        p = enc.packet(seed)
        im = b.generate(p.data, COLOR_NUMBER, a.version, a.ecc)
        img = Image.frombytes("RGBA", (im.width, im.height), im.rgba).convert("RGB")
        img = img.resize((im.width * a.scale, im.height * a.scale), Image.NEAREST)
        img = ImageOps.expand(img, 4 * a.scale, "white")
        img.save(os.path.join(a.outdir, f"frame_{i:05d}.png"))
    print(f"K={enc.k} block={block} cap={cap} frames={n} session={session:08X}")


if __name__ == "__main__":
    main()
