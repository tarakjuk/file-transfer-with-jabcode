const { execFileSync } = require('child_process');
const J = require('../receiver/jabcore.js');
// 파이썬 인코더가 만든 패킷 스트림을 받아 JS 디코더 성능/정확성 측정
for (const size of [100000, 1000000]) {
  const out = execFileSync('python3', ['-c', `
import os,sys,random
sys.path.insert(0,'../sender')
from jabfountain import *
data=os.urandom(${size}); c=build_container('big.bin',data,False); enc=FountainEncoder(c,328)
open('/tmp/big.bin','wb').write(data)
n=int(enc.k*1.03)+20; s=random.randrange(1<<30)
w=sys.stdout.buffer
for i in range(n):
    p=enc.packet(s+i*3).data; w.write(len(p).to_bytes(4,'little')+p)
`], { maxBuffer: 1 << 30 });
  let off = 0, dec = null, n = 0;
  const t0 = performance.now();
  while (off < out.length) {
    const l = out.readUInt32LE(off); const p = J.parsePacket(new Uint8Array(out.buffer, out.byteOffset + off + 4, l)); off += 4 + l;
    if (!dec) dec = new J.FountainDecoder(p.k, p.bs, p.total, p.session);
    dec.add(p); n++;
    if (dec.done) break;
  }
  const t1 = performance.now();
  const r = dec.result();
  const t2 = performance.now();
  J.parseContainer(r).then(c => {
    const ok = Buffer.compare(Buffer.from(c.data), require('fs').readFileSync('/tmp/big.bin')) === 0;
    console.log(`size ${size} K=${dec.k} packets ${n} (+${n - dec.k}) add ${(t1 - t0).toFixed(0)}ms (${((t1 - t0) / n).toFixed(2)}ms/pk) backsub ${(t2 - t1).toFixed(0)}ms ok=${ok}`);
  });
}
