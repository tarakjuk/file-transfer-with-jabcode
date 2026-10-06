"""
libjabcode ctypes 바인딩 (송신측 JAB Code 생성기).

우선순위:
  1) libjabcode.dll (Windows) / libjabcode.so (Linux) / libjabcode.dylib 를 ctypes로 직접 호출 (빠름)
  2) 실패 시 jabcodeWriter(.exe) 를 subprocess로 호출 (느림, 폴백)

생성 결과는 RGBA 바이트(모듈 1개 = 1픽셀)와 가로/세로 크기로 반환한다.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Optional

# 색 수는 8색 고정: 4색은 측정한 모든 조건에서 전송률이 8색보다 낮았고,
# 16색은 제공된 libjabcode.dll 의 generateJABCode 가 항상 access violation 을 일으킨다.
COLOR_NUMBER = 8

# 심볼 배치 (jabcode jab_symbol_pos 위치 번호). 2×2 다중 심볼 = 마스터(좌상) + 오른쪽(4)·아래(2)·오른쪽아래(10)
# 슬레이브. 슬레이브는 간격 없이 도킹되고 파인더 패턴이 없어, 디코더가 마스터 하나를 찾아 4칸을 한 번에 읽는다.
SINGLE = (0,)
MULTI_2X2 = (0, 4, 2, 10)

# ---------------------------------------------------------------- 구조체 정의 (jabcode.h v2.0.0)


class JabVector2d(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class JabBitmapHeader(ctypes.Structure):
    # jab_bitmap 의 고정부. 뒤에 pixel[] 이 바로 이어진다 (flexible array).
    _fields_ = [
        ("width", ctypes.c_int32),
        ("height", ctypes.c_int32),
        ("bits_per_pixel", ctypes.c_int32),
        ("bits_per_channel", ctypes.c_int32),
        ("channel_count", ctypes.c_int32),
    ]


class JabEncode(ctypes.Structure):
    _fields_ = [
        ("color_number", ctypes.c_int32),
        ("symbol_number", ctypes.c_int32),
        ("module_size", ctypes.c_int32),
        ("master_symbol_width", ctypes.c_int32),
        ("master_symbol_height", ctypes.c_int32),
        ("palette", ctypes.c_void_p),
        ("symbol_versions", ctypes.POINTER(JabVector2d)),
        ("symbol_ecc_levels", ctypes.POINTER(ctypes.c_ubyte)),
        ("symbol_positions", ctypes.POINTER(ctypes.c_int32)),
        ("symbols", ctypes.c_void_p),
        ("bitmap", ctypes.POINTER(JabBitmapHeader)),
    ]


@dataclass
class JabImage:
    width: int
    height: int
    rgba: bytes  # width*height*4, 1 module == 1 pixel


class JabCodeError(RuntimeError):
    pass


def _here() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _search_dirs():
    h = _here()
    env = os.environ.get("JABCODE_DIR")
    dirs = [env] if env else []
    p1 = os.path.dirname(h)
    dirs += [h, p1, os.path.dirname(p1), os.getcwd()]
    seen = []
    for d in dirs:
        if d and os.path.isdir(d) and d not in seen:
            seen.append(d)
    return seen


# ---------------------------------------------------------------- DLL 백엔드


class DllBackend:
    name = "libjabcode (ctypes)"

    def __init__(self, path: str):
        d = os.path.dirname(os.path.abspath(path))
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
        self.path = path
        self.lib = ctypes.CDLL(path)
        L = self.lib
        L.createEncode.argtypes = [ctypes.c_int32, ctypes.c_int32]
        L.createEncode.restype = ctypes.POINTER(JabEncode)
        L.destroyEncode.argtypes = [ctypes.POINTER(JabEncode)]
        L.destroyEncode.restype = None
        L.generateJABCode.argtypes = [ctypes.POINTER(JabEncode), ctypes.c_void_p]
        L.generateJABCode.restype = ctypes.c_int32

    def generate(self, data: bytes, color_number: int, version: int, ecc_level: int,
                 positions: tuple = SINGLE) -> JabImage:
        enc = self.lib.createEncode(color_number, len(positions))
        if not enc:
            raise JabCodeError("createEncode 실패")
        try:
            e = enc.contents
            e.module_size = 1
            for i, pos in enumerate(positions):          # 모든 심볼 같은 버전·ECC (도킹 조건: 맞닿는 변의 버전 일치)
                e.symbol_positions[i] = pos
                if version > 0:
                    e.symbol_versions[i].x = version
                    e.symbol_versions[i].y = version
                if ecc_level > 0:
                    e.symbol_ecc_levels[i] = ecc_level
            # jab_data { int32 length; char data[]; }
            buf = ctypes.create_string_buffer(4 + len(data))
            ctypes.memmove(buf, len(data).to_bytes(4, "little", signed=True), 4)
            ctypes.memmove(ctypes.addressof(buf) + 4, data, len(data))
            rc = self.lib.generateJABCode(enc, ctypes.addressof(buf))
            if rc != 0:
                raise JabCodeError(f"generateJABCode 실패 (code {rc}) - 데이터가 심볼 용량을 초과했을 수 있음")
            bmp = e.bitmap
            if not bmp:
                raise JabCodeError("bitmap 없음")
            hdr = bmp.contents
            w, h = hdr.width, hdr.height
            if hdr.channel_count != 4 or hdr.bits_per_pixel != 32:
                raise JabCodeError("예상치 못한 bitmap 포맷")
            pix_addr = ctypes.addressof(hdr) + ctypes.sizeof(JabBitmapHeader)
            rgba = ctypes.string_at(pix_addr, w * h * 4)
            return JabImage(w, h, rgba)
        finally:
            self.lib.destroyEncode(enc)


# ---------------------------------------------------------------- EXE 폴백 백엔드


class ExeBackend:
    name = "jabcodeWriter (subprocess)"

    def __init__(self, path: str):
        self.path = path
        self._tmp = tempfile.mkdtemp(prefix="jabtx_")

    def generate(self, data: bytes, color_number: int, version: int, ecc_level: int,
                 positions: tuple = SINGLE) -> JabImage:
        from PIL import Image  # 폴백 경로에서만 필요

        inp = os.path.join(self._tmp, "in.bin")
        out = os.path.join(self._tmp, "out.png")
        with open(inp, "wb") as f:
            f.write(data)
        n = len(positions)
        cmd = [self.path, "--input-file", inp, "--output", out,
               "--color-number", str(color_number), "--module-size", "1"]
        if n > 1:   # --symbol-number 가 먼저 와야 이후 인자를 심볼 수만큼 읽는다
            cmd += ["--symbol-number", str(n), "--symbol-position"] + [str(p) for p in positions]
        if version > 0:
            cmd += ["--symbol-version"] + [str(version)] * (2 * n)
        if ecc_level > 0:
            cmd += ["--ecc-level"] + [str(ecc_level)] * n
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, creationflags=flags)
        if r.returncode != 0 or not os.path.exists(out):
            raise JabCodeError("jabcodeWriter 실패: " + (r.stdout + r.stderr).decode(errors="ignore")[-300:])
        im = Image.open(out).convert("RGBA")
        os.remove(out)
        return JabImage(im.width, im.height, im.tobytes())


# ---------------------------------------------------------------- 로더


def load_backend(prefer: Optional[str] = None):
    """사용 가능한 백엔드를 찾아 반환. prefer='exe' 이면 EXE 우선."""
    errors = []
    libnames = ["libjabcode.dll"] if os.name == "nt" else ["libjabcode.so", "libjabcode.dylib", "libjabcode.dll"]
    exenames = ["jabcodeWriter.exe"] if os.name == "nt" else ["jabcodeWriter", "jabcodeWriter.exe"]

    def try_dll():
        for d in _search_dirs():
            for n in libnames:
                p = os.path.join(d, n)
                if os.path.exists(p):
                    try:
                        return DllBackend(p)
                    except OSError as ex:
                        errors.append(f"{p}: {ex}")
        return None

    def try_exe():
        for d in _search_dirs():
            for n in exenames:
                p = os.path.join(d, n)
                if os.path.exists(p):
                    return ExeBackend(p)
        return None

    order = [try_exe, try_dll] if prefer == "exe" else [try_dll, try_exe]
    for fn in order:
        b = fn()
        if b:
            return b
    raise JabCodeError("libjabcode / jabcodeWriter 를 찾을 수 없습니다.\n" + "\n".join(errors))


# ---------------------------------------------------------------- 용량 측정

_cap_cache: dict = {}


def probe_capacity(backend, color_number: int, version: int, ecc_level: int, positions: tuple = SINGLE) -> int:
    """주어진 설정에서 넣을 수 있는 최대 바이너리 바이트 수 (최악 조건: 전부 byte mode).
    이진 탐색으로 실제 generateJABCode 를 호출해 측정한다. 다중 심볼이면 전체 심볼 합계."""
    key = (backend.name, color_number, version, ecc_level, tuple(positions))
    if key in _cap_cache:
        return _cap_cache[key]
    import random
    rnd = random.Random(1234)
    worst = bytes(rnd.randrange(0x80, 0x100) for _ in range(32768))

    def fits(n):
        try:
            backend.generate(worst[:n], color_number, version, ecc_level, positions)
            return True
        except JabCodeError:
            return False

    lo, hi = 0, 64
    while fits(hi) and hi < 32768:
        lo, hi = hi, hi * 2
    hi = min(hi, 32768)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid
    _cap_cache[key] = lo
    return lo
