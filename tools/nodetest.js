// node nodetest.js <frames_dir> [outdir]  — WASM 디코더 + 파운틴 디코더 종단 테스트
const fs = require('fs'), path = require('path');
const { PNG } = require('pngjs');
const jpeg = require('jpeg-js');
const J = require('../receiver/jabcore.js');
(async () => {
  const jab = await J.loadJab(fs.readFileSync(path.join(__dirname, process.env.WASM || '../receiver/jabcode.wasm')));
  const dir = process.argv[2];
  const files = fs.readdirSync(dir).filter(f => /\.(png|jpe?g)$/i.test(f)).sort();
  let dec = null, ok = 0, fail = 0, tsum = 0;
  for (const f of files) {
    const buf = fs.readFileSync(path.join(dir, f));
    const img = /png$/i.test(f) ? PNG.sync.read(buf) : jpeg.decode(buf, { useTArray: true });
    const t0 = performance.now();
    const r = jab.decode(img.data, img.width, img.height);
    tsum += performance.now() - t0;
    if (!(r instanceof Uint8Array)) { fail++; continue; }
    const p = J.parsePacket(r);
    if (!p) { fail++; console.log('bad packet', f); continue; }
    ok++;
    if (!dec || dec.session !== p.session) dec = new J.FountainDecoder(p.k, p.bs, p.total, p.session);
    dec.add(p);
    if (dec.done) break;
  }
  console.log(`decoded ${ok}, failed ${fail}, avg ${(tsum / (ok + fail)).toFixed(1)} ms/frame, rank ${dec && dec.rank}/${dec && dec.k}, mem ${jab.memoryBytes() >> 20}MB`);
  if (dec && dec.done) {
    const c = await J.parseContainer(dec.result());
    console.log('FILE', c.name, c.data.length);
    if (process.argv[3]) fs.writeFileSync(path.join(process.argv[3], c.name), c.data);
  }
})();
