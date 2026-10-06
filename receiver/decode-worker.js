/* JAB Code 디코드 워커: 카메라 프레임(RGBA) -> libjabcode(WASM) -> 원시 패킷 바이트 */
importScripts('jabcore.js');
let jab = null, wasmModule = null, wasmUrl = null;

async function init() {
  if (!wasmModule) {
    const res = await fetch(wasmUrl);
    if (!res.ok) throw new Error('jabcode.wasm 로드 실패 (' + res.status + ')');
    wasmModule = await WebAssembly.compile(await res.arrayBuffer());
  }
  jab = await JabCore.loadJab(wasmModule);
}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (m.type === 'init') {
    wasmUrl = m.wasmUrl;
    try { await init(); self.postMessage({ type: 'ready' }); }
    catch (e) { self.postMessage({ type: 'fatal', msg: String(e && e.message || e) }); }
    return;
  }
  if (m.type === 'frame') {
    const t0 = performance.now();
    let out = null, status = 0, geom = null;
    try {
      if (!jab) await init();
      const r = jab.decodeEx(new Uint8Array(m.buf), m.w, m.h);
      out = r.data; status = r.status; geom = r.geom;
      // 메모리 누수 대비: 너무 커지면 인스턴스 재생성
      if (jab.memoryBytes() > 384 * 1024 * 1024) jab = null;
    } catch (e) {
      jab = null;   // wasm trap 등 → 다음 프레임에서 재생성
      status = -2;
    }
    const msg = { type: 'result', id: m.id, status, ms: performance.now() - t0, buf: m.buf, data: out ? out.buffer : null, geom };
    const tr = [m.buf];
    if (out) tr.push(out.buffer);
    self.postMessage(msg, tr);
  }
};
