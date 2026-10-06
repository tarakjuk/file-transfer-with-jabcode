/* 파운틴 디코드 워커: 패킷 -> 온라인 가우스 소거 -> 파일 복원 */
importScripts('jabcore.js', 'fflate.js');
const sessions = new Map();     // session -> FountainDecoder
const finished = new Set();     // 완료된 session
let active = null;

function progress(dec, extra) {
  self.postMessage(Object.assign({
    type: 'progress', session: dec.session, k: dec.k, rank: dec.rank, bs: dec.bs, total: dec.total,
    received: dec.received, useless: dec.useless,
  }, extra || {}));
}

self.onmessage = async (ev) => {
  const m = ev.data;
  if (m.type === 'reset') { sessions.clear(); finished.clear(); active = null; return; }
  if (m.type !== 'packet') return;
  const p = JabCore.parsePacket(new Uint8Array(m.data));
  if (!p) { self.postMessage({ type: 'invalid' }); return; }
  if (finished.has(p.session)) { self.postMessage({ type: 'dup-finished', session: p.session }); return; }
  let dec = sessions.get(p.session);
  if (!dec || dec.k !== p.k || dec.bs !== p.bs || dec.total !== p.total) {
    dec = new JabCore.FountainDecoder(p.k, p.bs, p.total, p.session);
    sessions.set(p.session, dec);
    // 오래된 미완료 세션은 4개까지만 유지
    if (sessions.size > 4) sessions.delete(sessions.keys().next().value);
  }
  const isNew = active !== p.session;
  active = p.session;
  const useful = dec.add(p);
  progress(dec, { useful, isNew });
  if (dec.done) {
    finished.add(p.session);
    sessions.delete(p.session);
    self.postMessage({ type: 'assembling', session: p.session });
    try {
      const buf = dec.result();
      const c = await JabCore.parseContainer(buf);
      const data = c.data.buffer.byteLength === c.data.length ? c.data.buffer : c.data.slice().buffer;
      self.postMessage({ type: 'file', session: p.session, name: c.name, data, frames: dec.received }, [data]);
    } catch (e) {
      finished.delete(p.session);
      self.postMessage({ type: 'error', session: p.session, msg: '파일 복원 실패: ' + (e.message || e) });
    }
  }
};
