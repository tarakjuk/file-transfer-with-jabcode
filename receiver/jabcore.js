/*
 * jabcore.js — 수신측 핵심 로직 (브라우저 워커 / Node 공용)
 *   - libjabcode WASM 로더 (WASI 최소 shim)
 *   - 패킷 파서 (CRC32 검증)
 *   - 파운틴 디코더: 온라인 GF(2) 가우스 소거
 *   - 컨테이너 파서 (파일명 / 압축 / CRC)
 * 송신측 jabfountain.py 와 비트 단위로 호환되어야 한다.
 */
(function (root) {
  'use strict';

  // ------------------------------------------------------------ CRC32
  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();
  function crc32(buf, start = 0, end = buf.length, crc = 0) {
    let c = ~crc >>> 0;
    for (let i = start; i < end; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
    return ~c >>> 0;
  }

  // ------------------------------------------------------------ PRNG (Python Mulberry32 과 동일)
  function mulberry32(seed) {
    let a = seed | 0;
    return function () {
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return (t ^ (t >>> 14)) >>> 0;
    };
  }
  function blockIndices(seed, degree, k, session) {
    degree = Math.max(1, Math.min(degree, k));
    const rng = mulberry32((seed ^ session) >>> 0);
    const out = [];
    const seen = new Set();
    while (out.length < degree) {
      const i = rng() % k;
      if (!seen.has(i)) { seen.add(i); out.push(i); }
    }
    return out;
  }

  // ------------------------------------------------------------ 패킷
  const HEADER_SIZE = 28;
  function parsePacket(u8) {
    if (!u8 || u8.length < HEADER_SIZE || u8[0] !== 0x4A || u8[1] !== 0x46) return null;
    const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    const ver = u8[2];
    if (ver !== 1) return null;
    const session = dv.getUint32(4, true);
    const k = dv.getUint32(8, true);
    const bs = dv.getUint16(12, true);
    const total = dv.getUint32(14, true);
    const seed = dv.getUint32(18, true);
    const degree = dv.getUint16(22, true);
    const crc = dv.getUint32(24, true);
    if (u8.length < HEADER_SIZE + bs || k === 0 || bs === 0) return null;
    let c = crc32(u8, 0, 24);
    c = crc32(u8, HEADER_SIZE, HEADER_SIZE + bs, c);
    if (c !== crc) return null;
    if (k * bs < total || (k - 1) * bs >= total + bs) return null;
    return { session, k, bs, total, seed, degree, layout: (u8[3] >> 2) & 3,
      payload: u8.subarray(HEADER_SIZE, HEADER_SIZE + bs) };
  }
  // flags.layout: 0 = 단일 심볼, 1 = JAB 2×2 다중 심볼 (마스터 좌상 + 슬레이브 3, 송신 jabfountain.py 와 일치)
  const LAYOUT_MULTI2 = 1;

  // ------------------------------------------------------------ 파운틴 디코더 (온라인 가우스 소거)
  class FountainDecoder {
    constructor(k, bs, total, session) {
      this.k = k; this.bs = bs; this.total = total; this.session = session;
      this.W = (k + 31) >>> 5;               // 마스크 워드 수
      this.D = (bs + 3) >>> 2;               // 데이터 워드 수
      this.masks = new Array(k).fill(null);  // 최상위 비트 h 를 갖는 피벗 행
      this.datas = new Array(k).fill(null);
      this.rank = 0;
      this.seen = new Set();
      this.received = 0;   // 유효 패킷 수 (중복 제외)
      this.useless = 0;    // 선형 종속이라 쓸모 없던 패킷 수
    }
    get done() { return this.rank === this.k; }

    add(p) {
      if (this.done || this.seen.has(p.seed)) return false;
      this.seen.add(p.seed);
      this.received++;
      const W = this.W, D = this.D;
      const m = new Uint32Array(W);
      for (const i of blockIndices(p.seed, p.degree, this.k, this.session)) m[i >>> 5] ^= (1 << (i & 31));
      const d = new Uint32Array(D);
      new Uint8Array(d.buffer).set(p.payload);
      let top = W - 1;
      for (;;) {
        while (top >= 0 && m[top] === 0) top--;
        if (top < 0) { this.useless++; return false; }   // 선형 종속
        const h = (top << 5) + (31 - Math.clz32(m[top]));
        const pm = this.masks[h];
        if (pm === null) {
          this.masks[h] = m; this.datas[h] = d; this.rank++;
          return true;
        }
        const pd = this.datas[h];
        for (let w = 0; w <= top; w++) m[w] ^= pm[w];
        for (let w = 0; w < D; w++) d[w] ^= pd[w];
      }
    }

    // rank == k 일 때 역대입으로 원본 복원
    result() {
      if (!this.done) return null;
      const k = this.k, D = this.D;
      const sol = new Array(k);
      for (let h = 0; h < k; h++) {
        const m = this.masks[h], d = this.datas[h];
        const hw = h >>> 5;
        for (let w = 0; w <= hw; w++) {
          let bits = m[w];
          if (w === hw) bits &= ~(1 << (h & 31)) | 0;   // 자기 자신 제외 (이하 비트만 존재)
          while (bits) {
            const b = 31 - Math.clz32(bits);
            bits &= ~(1 << b);
            const s = sol[(w << 5) + b];
            for (let x = 0; x < D; x++) d[x] ^= s[x];
          }
        }
        sol[h] = d;
      }
      const out = new Uint8Array(this.total);
      let off = 0;
      for (let i = 0; i < k && off < this.total; i++) {
        const u = new Uint8Array(sol[i].buffer, 0, Math.min(this.bs, this.total - off));
        out.set(u, off);
        off += u.length;
      }
      return out;
    }
  }

  // ------------------------------------------------------------ 컨테이너
  async function inflateZlib(u8) {
    if (typeof DecompressionStream !== 'undefined') {
      try {
        const ds = new DecompressionStream('deflate');
        const ab = await new Response(new Blob([u8]).stream().pipeThrough(ds)).arrayBuffer();
        return new Uint8Array(ab);
      } catch (e) { /* fallthrough */ }
    }
    if (root.fflate && root.fflate.unzlibSync) return root.fflate.unzlibSync(u8);
    throw new Error('이 브라우저는 압축 해제를 지원하지 않습니다');
  }

  async function parseContainer(buf) {
    if (buf.length < 20 || buf[0] !== 0x4A || buf[1] !== 0x46 || buf[2] !== 0x43 || buf[3] !== 0x31)
      throw new Error('컨테이너 형식 오류');
    const dv = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
    const flags = buf[4];
    const nlen = dv.getUint16(6, true);
    const osize = dv.getUint32(8, true);
    const crc = dv.getUint32(12, true);
    const blen = dv.getUint32(16, true);
    const name = new TextDecoder('utf-8').decode(buf.subarray(20, 20 + nlen));
    const body = buf.subarray(20 + nlen, 20 + nlen + blen);
    const data = (flags & 1) ? await inflateZlib(body) : body.slice();
    if (data.length !== osize) throw new Error('크기 불일치');
    if (crc32(data) !== crc) throw new Error('CRC 불일치');
    return { name, data };
  }

  // ------------------------------------------------------------ WASM 로더
  async function loadJab(wasmSource) {
    let mem = null;
    const dec = new TextDecoder();
    let logbuf = '';
    const wasi = {
      fd_write(fd, iovs, iovsLen, nwritten) {
        const dv = new DataView(mem.buffer);
        let n = 0;
        for (let i = 0; i < iovsLen; i++) {
          const p = dv.getUint32(iovs + i * 8, true), l = dv.getUint32(iovs + i * 8 + 4, true);
          logbuf += dec.decode(new Uint8Array(mem.buffer, p, l));
          n += l;
        }
        dv.setUint32(nwritten, n, true);
        if (logbuf.length > 4096) logbuf = logbuf.slice(-2048);
        return 0;
      },
      fd_close() { return 0; },
      fd_seek() { return 70; },          // ESPIPE
      fd_fdstat_get(fd, out) {
        const dv = new DataView(mem.buffer);
        for (let i = 0; i < 24; i++) dv.setUint8(out + i, 0);
        dv.setUint8(out, 2);               // character device
        return 0;
      },
      random_get(p, l) {
        const u = new Uint8Array(mem.buffer, p, l);
        if (typeof crypto !== 'undefined' && crypto.getRandomValues) crypto.getRandomValues(u);
        else for (let i = 0; i < l; i++) u[i] = (Math.random() * 256) | 0;
        return 0;
      },
      proc_exit(c) { throw new Error('wasm exit ' + c); },
    };
    let module;
    if (wasmSource instanceof WebAssembly.Module) module = wasmSource;
    else module = await WebAssembly.compile(wasmSource);
    const inst = await WebAssembly.instantiate(module, { wasi_snapshot_preview1: wasi });
    mem = inst.exports.memory;
    inst.exports._initialize && inst.exports._initialize();
    const ex = inst.exports;
    let geomPtr = 0;
    return {
      module,
      memoryBytes() { return mem.buffer.byteLength; },
      log() { const l = logbuf; logbuf = ''; return l; },
      /** rgba: Uint8Array/Uint8ClampedArray (w*h*4). 성공 시 Uint8Array, 실패 시 {status} */
      decode(rgba, w, h) {
        const r = this.decodeEx(rgba, w, h);
        return r.data || { status: r.status };
      },
      /** {data: Uint8Array|null, status, geom: {side:[x,y], module, fp:[[x,y]×4]}|null}
       *  geom 은 파인더 패턴 4개를 찾았으면 디코드 실패여도 채워진다 (이미지 픽셀 좌표). */
      decodeEx(rgba, w, h) {
        const ptr = ex.jw_buffer(w, h);
        if (!ptr) return { data: null, status: -1, geom: null };
        new Uint8Array(mem.buffer, ptr, w * h * 4).set(rgba);
        const n = ex.jw_decode(0);
        let data = null, status;
        if (n < 0) status = -1 - n;
        else { status = 3; data = new Uint8Array(mem.buffer, ex.jw_result(), n).slice(); }
        let geom = null;
        if (ex.jw_geom) {
          if (!geomPtr) geomPtr = ex.malloc(64);
          if (ex.jw_geom(geomPtr)) {
            const f = new Float32Array(mem.buffer, geomPtr, 11);
            geom = { side: [f[0], f[1]], module: f[2], fp: [[f[3], f[4]], [f[5], f[6]], [f[7], f[8]], [f[9], f[10]]] };
          }
        }
        return { data, status, geom };
      },
    };
  }

  const api = { crc32, mulberry32, blockIndices, parsePacket, FountainDecoder, parseContainer, loadJab, HEADER_SIZE, LAYOUT_MULTI2 };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.JabCore = api;
})(typeof self !== 'undefined' ? self : globalThis);
