"""
프레임 생성 워커 (multiprocessing).

libjabcode 는 내부 전역 PRNG 상태(lcg64_seed)를 갖고 있어 스레드 안전하지 않다.
그래서 프로세스 풀을 사용해 프로세스마다 DLL 을 따로 로드한다.
"""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor

from jabcode_backend import load_backend
from jabfountain import FountainEncoder

_G: dict = {}


def _init(prefer, container, block_size, session, colors, version, ecc, positions, flags):
    _G["backend"] = load_backend(prefer)
    _G["enc"] = FountainEncoder(container, block_size, session)
    _G["params"] = (colors, version, ecc, positions, flags)


def _make(seed: int):
    """표시 프레임 하나 = 패킷 1개 → (seed, degree, w, h, rgba). 다중 심볼이면 4칸이 한 장에 그려진다."""
    enc = _G["enc"]
    c, v, e, positions, flags = _G["params"]
    pkt = enc.packet(seed, flags)
    im = _G["backend"].generate(pkt.data, c, v, e, positions)
    return seed, pkt.degree, im.width, im.height, im.rgba


def default_workers() -> int:
    n = os.cpu_count() or 2
    return max(1, min(6, n - 1))


def make_pool(prefer, container, block_size, session, colors, version, ecc, positions, flags, workers=None):
    return ProcessPoolExecutor(
        max_workers=workers or default_workers(),
        initializer=_init,
        initargs=(prefer, container, block_size, session, colors, version, ecc, positions, flags),
    )


def make_frame(seed):
    return _make(seed)
