"""
JAB 광학 전송 프로토콜 + LT 파운틴 코드 (송신측).

[컨테이너] 파일 1개를 아래 바이트열로 감싼 뒤 K개의 블록으로 나눈다.
    0  4  magic  b"JFC1"
    4  1  flags  bit0 = zlib 압축
    5  1  reserved
    6  2  name_len (u16)
    8  4  원본 파일 크기 (u32)
   12  4  원본 파일 CRC32 (u32)
   16  4  저장 데이터 길이 (u32)
   20  .  파일명 (UTF-8) + 데이터

[패킷] JAB Code 한 프레임 = 패킷 1개 (리틀엔디언)
    0  2  magic b"JF"
    2  1  protocol version (1)
    3  1  flags  bit2-3 = 배치 (0=단일 심볼, 1=JAB 2×2 다중 심볼 — 마스터 좌상, 슬레이브 오른쪽·아래·오른쪽아래)
                 나머지 비트 0 (예약)
    4  4  session id (u32, 파일 내용+설정에서 결정)
    8  4  K  (소스 블록 수)
   12  2  block size
   14  4  컨테이너 전체 길이
   18  4  seed (= 패킷 번호)
   22  2  degree
   24  4  CRC32(헤더[0:24] + payload)
   28  .  payload (block size 바이트)

[파운틴 코드 (LT 계열 + GF(2) 가우스 소거 복호)]
  모든 패킷은 소스 블록 degree 개를 XOR 한 값이다.
  - degree 는 송신측이 [lo, hi] 균등분포(lo≈ln(K)/2+2, hi≈2ln(K)+4, 홀짝 혼합)에서 뽑아 헤더에 기록.
    (짝수 degree만 쓰면 GF(2)에서 rank K-1 이 되어 복구 불가 → 반드시 홀짝 혼합)
  - 블록 인덱스는 mulberry32(seed ^ session) 로 결정 → 수신측이 동일하게 재현.
  - 수신측은 패킷이 들어올 때마다 온라인 가우스 소거를 수행하므로 어떤 프레임을 놓쳐도
    대략 K + (0~수 %) 개의 서로 다른 프레임만 받으면 복원된다. (시뮬레이션: K≥100 에서 오버헤드 ~1%)
  degree를 헤더에 넣었으므로 수신측은 분포를 계산할 필요가 없다 (부동소수 불일치 위험 제거).

[2×2 다중 심볼] JAB Code 자체의 다중 심볼(마스터 1 + 도킹된 슬레이브 3, 간격 없음)로 한 프레임을 만든다.
  패킷 1개가 4칸에 나뉘어 담기고(프레임당 패킷 1개, seed = 프레임 번호), 디코더는 파인더 패턴이 있는
  마스터 하나를 찾아 슬레이브까지 한 번에 읽는다. 한 칸이라도 실패하면 그 프레임은 통째로 실패한다.
  코드 한 변 = 2×S 모듈 (S = 심볼 한 변). 수신측은 flags 의 배치로 마스터 위치에서 코드 전체 범위를 계산한다.
"""
from __future__ import annotations

import math
import os
import random
import struct
import zlib
from dataclasses import dataclass

PACKET_MAGIC = b"JF"
PROTO_VERSION = 1
HEADER_FMT = "<2sBBIIHIIH"   # CRC 제외 24바이트
HEADER_SIZE = 28
CONTAINER_MAGIC = b"JFC1"
CONTAINER_HDR = "<4sBBHIII"  # 20바이트

M32 = 0xFFFFFFFF

LAYOUT_SINGLE, LAYOUT_MULTI2 = 0, 1


def packet_flags(layout: int) -> int:
    return (layout & 3) << 2


# ---------------------------------------------------------------- PRNG (JS 와 비트 단위 동일)


class Mulberry32:
    def __init__(self, seed: int):
        self.a = seed & M32

    def next(self) -> int:
        self.a = (self.a + 0x6D2B79F5) & M32
        a = self.a
        t = ((a ^ (a >> 15)) * (1 | a)) & M32
        t = ((t + (((t ^ (t >> 7)) * (61 | t)) & M32)) & M32) ^ t
        return (t ^ (t >> 14)) & M32


def block_indices(seed: int, degree: int, k: int, session: int) -> list[int]:
    """패킷 seed 가 XOR 하는 소스 블록 인덱스 목록 (수신측 JS 구현과 동일해야 함)."""
    degree = max(1, min(degree, k))
    rng = Mulberry32(seed ^ session)
    out: list[int] = []
    seen = set()
    while len(out) < degree:
        i = rng.next() % k
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ---------------------------------------------------------------- degree 분포


def degree_range(k: int) -> tuple[int, int]:
    if k <= 8:
        return 1, k
    lnk = math.log(k)
    lo = int(lnk / 2) + 2
    hi = min(k, int(2 * lnk) + 4)
    return lo, max(lo + 1, hi)


# ---------------------------------------------------------------- 컨테이너


def build_container(filename: str, data: bytes, compress: bool = True) -> bytes:
    name_b = os.path.basename(filename).encode("utf-8")
    if len(name_b) > 1024:
        # 너무 긴 파일명은 확장자를 보존하며 자른다
        root, ext = os.path.splitext(os.path.basename(filename))
        name_b = (root.encode("utf-8")[: 1000].decode("utf-8", "ignore") + ext).encode("utf-8")
    crc = zlib.crc32(data) & M32
    flags = 0
    body = data
    if compress and len(data) > 64:
        z = zlib.compress(data, 9)
        if len(z) < len(data) * 0.95:
            body, flags = z, 1
    hdr = struct.pack(CONTAINER_HDR, CONTAINER_MAGIC, flags, 0, len(name_b), len(data), crc, len(body))
    return hdr + name_b + body


# ---------------------------------------------------------------- 인코더


@dataclass
class Packet:
    seed: int
    degree: int
    data: bytes  # 헤더 포함 전체 패킷


class FountainEncoder:
    def __init__(self, container: bytes, block_size: int, session: int | None = None):
        if block_size < 8 or block_size > 65535:
            raise ValueError("block size 범위 오류")
        self.container = container
        self.block_size = block_size
        self.total_len = len(container)
        self.k = max(1, math.ceil(self.total_len / block_size))
        pad = self.k * block_size - self.total_len
        padded = container + b"\x00" * pad
        self.blocks = [int.from_bytes(padded[i * block_size:(i + 1) * block_size], "little")
                       for i in range(self.k)]
        self.session = (session if session is not None else random.getrandbits(32)) & M32
        if self.session == 0:
            self.session = 1
        self.deg_lo, self.deg_hi = degree_range(self.k)

    def _sample_degree(self, seed: int) -> int:
        return random.Random((self.session << 32) | seed).randint(self.deg_lo, self.deg_hi)

    def packet(self, seed: int, flags: int = 0) -> Packet:
        seed &= M32
        degree = self._sample_degree(seed)
        idx = block_indices(seed, degree, self.k, self.session)
        acc = 0
        for i in idx:
            acc ^= self.blocks[i]
        payload = acc.to_bytes(self.block_size, "little")
        hdr = struct.pack(HEADER_FMT, PACKET_MAGIC, PROTO_VERSION, flags & 0xFF, self.session, self.k,
                          self.block_size, self.total_len, seed, len(idx))
        crc = zlib.crc32(hdr + payload) & M32
        return Packet(seed, len(idx), hdr + struct.pack("<I", crc) + payload)


# ---------------------------------------------------------------- 디코더 (테스트/검증용 파이썬 구현)


def parse_packet(raw: bytes):
    if len(raw) < HEADER_SIZE or raw[:2] != PACKET_MAGIC:
        return None
    magic, ver, flags, session, k, bs, total, seed, deg = struct.unpack(HEADER_FMT, raw[:24])
    (crc,) = struct.unpack("<I", raw[24:28])
    payload = raw[28:28 + bs]
    if ver != PROTO_VERSION or len(payload) != bs:
        return None
    if zlib.crc32(raw[:24] + payload) & M32 != crc:
        return None
    return dict(session=session, k=k, bs=bs, total=total, seed=seed, degree=deg, payload=payload,
                layout=(flags >> 2) & 3)


class FountainDecoder:
    """수신측 JS 디코더와 같은 알고리즘(온라인 GF(2) 가우스 소거)의 파이썬 버전. 시뮬레이션/검증용."""

    def __init__(self, k, bs, total, session):
        self.k, self.bs, self.total, self.session = k, bs, total, session
        self.piv: dict[int, list] = {}  # 최상위 비트 -> [mask, value]
        self.seen = set()

    def add(self, p) -> bool:
        if p["seed"] in self.seen or self.done:
            return self.done
        self.seen.add(p["seed"])
        m = 0
        for i in block_indices(p["seed"], p["degree"], self.k, self.session):
            m |= 1 << i
        v = int.from_bytes(p["payload"], "little")
        while m:
            h = m.bit_length() - 1
            if h in self.piv:
                pm, pv = self.piv[h]
                m ^= pm
                v ^= pv
            else:
                self.piv[h] = [m, v]
                break
        return self.done

    @property
    def rank(self):
        return len(self.piv)

    @property
    def done(self):
        return len(self.piv) == self.k

    def result(self) -> bytes:
        # 역대입: 낮은 비트부터 해를 확정
        sol = [0] * self.k
        for h in range(self.k):
            m, v = self.piv[h]
            rest = m & ~(1 << h)
            while rest:
                low = rest & -rest
                v ^= sol[low.bit_length() - 1]
                rest ^= low
            sol[h] = v
        b = b"".join(x.to_bytes(self.bs, "little") for x in sol)
        return b[: self.total]


def parse_container(buf: bytes):
    magic, flags, _r, nlen, osize, crc, blen = struct.unpack(CONTAINER_HDR, buf[:20])
    if magic != CONTAINER_MAGIC:
        raise ValueError("container magic 불일치")
    name = buf[20:20 + nlen].decode("utf-8")
    body = buf[20 + nlen:20 + nlen + blen]
    data = zlib.decompress(body) if flags & 1 else body
    if len(data) != osize or (zlib.crc32(data) & M32) != crc:
        raise ValueError("CRC/크기 불일치")
    return name, data
