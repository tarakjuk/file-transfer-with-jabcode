/* JAB 광학 수신기 — 메인 스레드 (카메라, 프레임 분배, UI) */
(() => {
  'use strict';
  const APP_VERSION = 'v3.0';
  const $ = (id) => document.getElementById(id);
  const video = $('video'), guide = $('guide'), stage = $('stage'), cap = $('cap');
  const ctx = cap.getContext('2d', { willReadFrequently: true });
  $('ver').textContent = APP_VERSION;

  // ------------------------------------------------------------------ 상태
  const S = {
    multi: true,           // 송신 배치: 2×2 다중 심볼(기본) / 단일. 디코드된 패킷 flags 로 확정, 그 전엔 넓은 쪽(다중)으로 가정
    mode: null,            // 'camera' | 'file' | null
    stream: null,
    track: null,
    workers: [],           // {w, busy, ready}
    frameSeq: 0,           // 비디오 새 프레임 카운터
    lastSent: -1,
    analyzed: [],          // 최근 분석 완료 시각
    hits: [],              // 최근 분석 결과 (1/0)
    rankTimes: [],         // 유효 패킷 수신 시각
    cur: null,             // 현재 진행 정보 (fountain progress)
    received: new Map(),   // session -> file 정보
    rateT0: new Map(),     // session -> 첫 패킷 인식 시각 (전송률 기준)
    rateT1: new Map(),     // session -> 모든 블록 수신 시각
    wakeLock: null,
    rvfc: 'requestVideoFrameCallback' in HTMLVideoElement.prototype,
  };

  // ------------------------------------------------------------------ 유틸
  const human = (n) => {
    if (n == null) return '-';
    const u = ['B', 'KB', 'MB', 'GB']; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n.toFixed(0)) + ' ' + u[i];
  };
  const fmtTime = (s) => {
    if (!isFinite(s) || s < 0) return '-';
    s = Math.round(s);
    if (s < 60) return s + '초';
    const m = Math.floor(s / 60);
    return m < 60 ? `${m}분 ${s % 60}초` : `${Math.floor(m / 60)}시간 ${m % 60}분`;
  };
  function setStatus(t, err) { const el = $('status'); el.textContent = t; el.classList.toggle('err', !!err); }
  function notice(html) { const n = $('notice'); n.innerHTML = html; n.style.display = html ? 'block' : 'none'; }

  // ------------------------------------------------------------------ 환경 점검
  (function checkEnv() {
    const ua = navigator.userAgent || '';
    const here = location.href;
    if (/KAKAOTALK/i.test(ua)) {
      notice(`카카오톡 내장 브라우저에서는 카메라가 제한될 수 있습니다. <a href="kakaotalk://web/openExternal?url=${encodeURIComponent(here)}">기본 브라우저로 열기</a>`);
    } else if (/NAVER|Instagram|FBAN|FBAV|Line\//i.test(ua)) {
      notice('앱 내장 브라우저에서는 카메라가 동작하지 않을 수 있습니다. 메뉴에서 “다른 브라우저로 열기”를 선택하세요 (Chrome / Safari 권장).');
    } else if (!window.isSecureContext) {
      notice('카메라는 HTTPS 주소에서만 사용할 수 있습니다.');
    } else if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
      notice('이 브라우저는 실시간 카메라를 지원하지 않습니다. 기본 카메라 앱으로 화면을 동영상 촬영한 뒤 “🎞️ 동영상/사진”으로 불러오세요.');
    }
    if (typeof WebAssembly === 'undefined' || typeof Worker === 'undefined') {
      notice('이 브라우저는 WebAssembly / Web Worker를 지원하지 않아 사용할 수 없습니다. 최신 Chrome 또는 Safari를 사용하세요.');
      $('btnStart').disabled = true;
    }
  })();

  // ------------------------------------------------------------------ 워커
  const JC = window.JabCore;
  const hc = navigator.hardwareConcurrency || 2;
  const defWorkers = Math.max(1, Math.min(4, hc - 1));
  for (let i = 1; i <= Math.max(1, Math.min(8, hc)); i++) {
    const o = document.createElement('option');
    o.value = i; o.textContent = i + '개' + (i === defWorkers ? ' (기본)' : '');
    if (i === defWorkers) o.selected = true;
    $('selWorkers').appendChild(o);
  }

  const fountain = new Worker('fountain-worker.js');
  fountain.onmessage = (ev) => onFountain(ev.data);

  function spawnWorkers(n) {
    for (const x of S.workers) x.w.terminate();
    S.workers = [];
    const wasmUrl = new URL('jabcode.wasm', location.href).href;
    for (let i = 0; i < n; i++) {
      const x = { w: new Worker('decode-worker.js'), busy: true, ready: false };
      x.w.onmessage = (ev) => onDecode(x, ev.data);
      x.w.onerror = (e) => { setStatus('디코더 오류: ' + (e.message || e), true); };
      x.w.postMessage({ type: 'init', wasmUrl });
      S.workers.push(x);
    }
  }
  spawnWorkers(defWorkers);
  $('selWorkers').onchange = () => spawnWorkers(+$('selWorkers').value);

  function idleWorker() { return S.workers.find((x) => x.ready && !x.busy); }

  function onDecode(x, m) {
    if (m.type === 'ready') { x.ready = true; x.busy = false; pump(); return; }
    if (m.type === 'fatal') { setStatus('디코더 초기화 실패: ' + m.msg, true); return; }
    if (m.type !== 'result') return;
    x.busy = false;
    const now = performance.now();
    S.analyzed.push(now);
    const meta = S.frameMeta.get(m.id);
    S.frameMeta.delete(m.id);
    const ok = !!m.data;
    const pkt = ok ? JC.parsePacket(new Uint8Array(m.data)) : null;
    if (pkt) { S.multi = pkt.layout === JC.LAYOUT_MULTI2; ROI.nodata = 0; }   // 송신 배치 (단일 / 2×2 다중 심볼)
    const quad = m.geom && meta && meta.video ? codeQuad(meta, m.geom, ok) : null;
    roiResult(meta, m.geom, quad, ok);
    if (quad) applyQuad(meta, quad, ok, S.multi);
    S.hits.push(ok ? 1 : 0);
    if (ok) fountain.postMessage({ type: 'packet', data: m.data }, [m.data]);
    while (S.hits.length > 40) S.hits.shift();
    if (S.fileWaiter) { const f = S.fileWaiter; S.fileWaiter = null; f(); }
    pump();
  }

  // ------------------------------------------------------------------ JAB Code 추적 박스
  // 워커가 돌려준 파인더 패턴 4개(심볼 모듈 좌표 3.5 / side-3.5 위치)로 호모그래피를 구해
  // 심볼의 실제 네 모서리를 계산하고, 비디오 → 화면 좌표로 변환해 오버레이 캔버스에 그린다.
  const ov = $('ov'), octx = ov.getContext('2d');
  const TRK = { target: null, shown: null, ok: false, t: 0, applied: 0, capT: 0, vel: null, raf: 0, rej: 0, okSide: null };
  const FADE = 700;                         // 마지막 위치 결과 후 박스가 사라지기까지 (ms)
  const VALID_SIDES = Array.from({ length: 32 }, (_, i) => 21 + 4 * i);   // 버전 1~32 의 한 변 모듈 수

  function homography(src, dst) {           // src[i] -> dst[i], 4점
    const A = [], b = [];
    for (let i = 0; i < 4; i++) {
      const [x, y] = src[i], [u, v] = dst[i];
      A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.push(u);
      A.push([0, 0, 0, x, y, 1, -v * x, -v * y]); b.push(v);
    }
    for (let c = 0; c < 8; c++) {           // 가우스 소거 (부분 피벗)
      let p = c;
      for (let r = c + 1; r < 8; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
      [A[c], A[p]] = [A[p], A[c]]; [b[c], b[p]] = [b[p], b[c]];
      if (Math.abs(A[c][c]) < 1e-9) return null;
      for (let r = 0; r < 8; r++) if (r !== c) {
        const f = A[r][c] / A[c][c];
        for (let k = c; k < 8; k++) A[r][k] -= f * A[c][k];
        b[r] -= f * b[c];
      }
    }
    const h = b.map((v, i) => v / A[i][i]);
    return ([x, y]) => { const d = h[6] * x + h[7] * y + 1; return [(h[0] * x + h[1] * y + h[2]) / d, (h[3] * x + h[4] * y + h[5]) / d]; };
  }

  // 파인더 패턴 간격 / 모듈 크기로 심볼 한 변 모듈 수 추정 (libjabcode calculateSideSize 와 같은 방식)
  function sideFromFp(fp, module) {
    const n = (a, b) => {
      const dx = fp[b][0] - fp[a][0], dy = fp[b][1] - fp[a][1], d = Math.hypot(dx, dy);
      return (d * d) / (module * Math.max(Math.abs(dx), Math.abs(dy))) + 7;
    };
    const snap = (v) => VALID_SIDES.reduce((a, b) => (Math.abs(b - v) < Math.abs(a - v) ? b : a));
    return [snap((n(0, 1) + n(3, 2)) / 2), snap((n(0, 3) + n(1, 2)) / 2)];
  }
  const quadArea = (q) => q.reduce((a, p, i) => { const n = q[(i + 1) % 4]; return a + p[0] * n[1] - n[0] * p[1]; }, 0) / 2;
  function isConvex(q) {
    let sg = 0;
    for (let i = 0; i < 4; i++) {
      const a = q[i], b = q[(i + 1) % 4], c = q[(i + 2) % 4];
      const z = Math.sign((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]));
      if (!z || (sg && z !== sg)) return false;
      sg = z;
    }
    return true;
  }
  const centroid = (q) => [(q[0][0] + q[1][0] + q[2][0] + q[3][0]) / 4, (q[0][1] + q[1][1] + q[2][1] + q[3][1]) / 4];

  /** 마스터 심볼 위치(geom: 분석 이미지 좌표) → 코드 전체의 네 모서리 (비디오 좌표).
   *  2×2 다중 심볼(S.multi)은 마스터가 좌상이고 같은 크기 슬레이브가 간격 없이 오른쪽·아래에 붙으므로
   *  마스터 모듈 좌표 (0,0)~(2S,2S) 가 코드 전체다. libjabcode 의 geom 은 항상 마스터 심볼이다. */
  function codeQuad(meta, geom, ok) {
    // 심볼 크기: 디코드 성공 프레임의 값(메타데이터)만 신뢰한다. 검출만 된 프레임은 libjabcode 가
    // 메타데이터 해독에 실패하면 side 를 VERSION2SIZE(0)=17 로 덮어써 박스가 ~46% 커지므로,
    // 마지막 성공값(파인더 패턴 추정과 2버전 이내일 때) 또는 파인더 패턴 추정값을 쓴다.
    let [sx, sy] = geom.side;
    if (ok) TRK.okSide = [sx, sy];
    else {
      if (!(geom.module > 0)) return null;
      const est = sideFromFp(geom.fp, geom.module), c = TRK.okSide;
      [sx, sy] = c && Math.abs(c[0] - est[0]) <= 8 && Math.abs(c[1] - est[1]) <= 8 ? c : est;
    }
    const H = homography([[3.5, 3.5], [sx - 3.5, 3.5], [sx - 3.5, sy - 3.5], [3.5, sy - 3.5]], geom.fp);
    if (!H) return null;
    const k = S.multi ? 2 : 1;
    // 코드 네 모서리 (마스터 모듈 좌표) → 분석 이미지 픽셀 → 비디오 픽셀
    const quad = [[0, 0], [k * sx, 0], [k * sx, k * sy], [0, k * sy]].map((p) => {
      const [x, y] = H(p);
      return [meta.sx + x / meta.sc, meta.sy + y / meta.sc];
    });
    return quad.every((p) => isFinite(p[0]) && isFinite(p[1])) ? quad : null;
  }

  /** 추적 박스 목표 사각형 갱신 (비디오 좌표). multi = 2×2 다중 심볼 코드 전체 */
  function applyQuad(meta, quad, ok, multi) {
    if (!S.mode || meta.t < TRK.applied) return;   // 늦게 도착한 오래된 결과는 무시
    if (!!TRK.multi !== multi) { TRK.target = TRK.shown = TRK.vel = null; }   // 단일 ↔ 다중 전환 시 새로 시작
    TRK.multi = multi;
    // 오검출 거부: 볼록하지 않은 사각형, 또는 면적이 직전 대비 급변(2회 연속이면 실제 변화로 보고 수용)
    if (!isConvex(quad)) return;
    if (TRK.target && performance.now() - TRK.t < FADE) {
      const r = Math.abs(quadArea(quad) / quadArea(TRK.target));
      if ((r > 1.35 || r < 0.74) && ++TRK.rej < 2) return;
    }
    TRK.rej = 0;
    // 등속 예측용 속도 (분석 지연만큼 박스가 뒤처지는 것을 보정): 모서리별이 아닌 중심 이동 속도,
    // 초당 심볼 한 변의 2배로 상한 → 모서리 잡음이 예측으로 증폭되지 않게 한다.
    const prev = TRK.target, dtc = meta.t - TRK.applied;
    if (prev && TRK.applied && dtc > 20 && dtc < 600) {
      const c0 = centroid(prev), c1 = centroid(quad);
      let v = [(c1[0] - c0[0]) / dtc, (c1[1] - c0[1]) / dtc];
      if (TRK.vel) v = [TRK.vel[0] * 0.4 + v[0] * 0.6, TRK.vel[1] * 0.4 + v[1] * 0.6];
      const lim = (2 * Math.sqrt(Math.abs(quadArea(quad)))) / 1000, m = Math.hypot(v[0], v[1]);
      TRK.vel = m > lim ? [(v[0] * lim) / m, (v[1] * lim) / m] : v;
    } else TRK.vel = null;
    TRK.applied = meta.t;
    TRK.capT = meta.t;
    TRK.target = quad; TRK.ok = ok; TRK.t = performance.now();
    if (!TRK.shown) TRK.shown = quad.map((p) => p.slice());
    if (!TRK.raf) TRK.raf = requestAnimationFrame(drawTrack);
  }

  // ------------------------------------------------------------------ ROI 추적 (관심 영역 분석)
  // 전체 화면을 분석 크기(기본 1000px)로 줄이면 1080p 에서 0.52배가 되어, 작게 보이는 코드는 모듈이
  // 3px 미만으로 떨어져 검출이 끊긴다. 코드를 한 번 찾으면 다음 프레임부터는 파인더 패턴 주변만
  // 원해상도로 잘라 분석한다. 못 찾는 동안에는 전체 화면(축소)과 정사각 타일(거의 원해상도)을 번갈아 탐색.
  const ROI = { c: null, side: 0, t: 0, capT: 0, miss: 0, acq: 0, nodata: 0 };
  const ROI_SCALE = 2.2;     // 단일 심볼: ROI 한 변 = 파인더 패턴 간격 × 2.2 (심볼 ≈ ×1.17, 나머지는 이동 여유)
  const ROI_MULTI = 1.35;    // 2×2 다중 심볼: ROI 한 변 = 코드 전체 크기 × 1.35
  const ROI_NODATA = 8;      // 위치는 잡히는데 이만큼 연속 못 읽으면 다중 심볼일 수 있다고 보고 넓게 본다
  const ROI_MIN = 320;       // 최소 ROI 한 변 (비디오 px)
  const ROI_HOLD = 700;      // 이 시간 동안 위치 결과가 없으면 탐색 모드로
  const ROI_MISS = 4;        // ROI 프레임이 연속 이만큼 실패하면 탐색 모드로
  const ACQ_ORDER = [-1, 0.5, 0, -1, 0.5, 1];   // 탐색 순서: -1 = 전체 화면, 0~1 = 긴 축 위 정사각 타일 위치

  function roiReset() { ROI.c = null; ROI.miss = 0; ROI.capT = 0; }

  function roiResult(meta, geom, quad, ok) {
    if (!meta || !meta.video || !S.mode) return;
    if (!geom) {
      if (meta.kind === 'roi' && ROI.c) ROI.miss++;
      return;
    }
    // 단일 심볼로 알고 있는데 마스터만 계속 잡히고 못 읽으면 (송신이 다중 심볼로 바뀐 경우 등) 넓게 본다.
    // 다중 심볼을 단일 ROI 로 자르면 슬레이브가 잘려 영원히 못 읽기 때문.
    if (!ok && ++ROI.nodata >= ROI_NODATA && !S.multi) { S.multi = true; ROI.nodata = 0; }
    if (meta.t < ROI.capT) return;                       // 더 최근 프레임으로 이미 갱신됨
    if (S.multi) {
      if (!quad) return;                                 // 코드 전체(마스터 기준 2S×2S) + 여유
      const xs = quad.map((p) => p[0]), ys = quad.map((p) => p[1]);
      const ext = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
      if (!(ext > 16)) return;
      ROI.c = [xs.reduce((a, b) => a + b) / 4, ys.reduce((a, b) => a + b) / 4];
      ROI.side = Math.max(ROI_MIN, ext * ROI_MULTI);
    } else {
      const xs = geom.fp.map((p) => meta.sx + p[0] / meta.sc), ys = geom.fp.map((p) => meta.sy + p[1] / meta.sc);
      const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
      if (!(span > 8)) return;
      ROI.c = [xs.reduce((a, b) => a + b) / 4, ys.reduce((a, b) => a + b) / 4];
      ROI.side = Math.max(ROI_MIN, span * ROI_SCALE);
    }
    ROI.t = performance.now(); ROI.capT = meta.t; ROI.miss = 0;
  }

  /** 다음 분석 영역 {sx, sy, sw, sh, kind} (비디오 좌표) */
  function chooseRegion(vw, vh) {
    if (ROI.c && (performance.now() - ROI.t > ROI_HOLD || ROI.miss >= ROI_MISS)) roiReset();
    if (ROI.c) {
      const w = Math.min(ROI.side, vw), h = Math.min(ROI.side, vh);
      const x = Math.max(0, Math.min(vw - w, ROI.c[0] - w / 2)), y = Math.max(0, Math.min(vh - h, ROI.c[1] - h / 2));
      return (ROI.last = { sx: Math.round(x), sy: Math.round(y), sw: Math.round(w), sh: Math.round(h), kind: 'roi' });
    }
    if (guideFrac() < 1) {
      const side = Math.round(guideFrac() * Math.min(vw, vh));
      return { sx: Math.round((vw - side) / 2), sy: Math.round((vh - side) / 2), sw: side, sh: side, kind: 'guide' };
    }
    const step = ACQ_ORDER[ROI.acq++ % ACQ_ORDER.length];
    if (step < 0) return { sx: 0, sy: 0, sw: vw, sh: vh, kind: 'full' };
    const side = Math.min(vw, vh);
    const off = Math.round(step * (Math.max(vw, vh) - side));
    return vw >= vh ? { sx: off, sy: 0, sw: side, sh: side, kind: 'tile' } : { sx: 0, sy: off, sw: side, sh: side, kind: 'tile' };
  }

  function videoToScreen() {
    const vw = video.videoWidth, vh = video.videoHeight, cw = stage.clientWidth, ch = stage.clientHeight;
    const sc = Math.min(cw / vw, ch / vh);
    return { sc, ox: (cw - vw * sc) / 2, oy: (ch - vh * sc) / 2 };
  }

  let lastDraw = 0;
  function drawTrack(now) {
    TRK.raf = 0;
    const dpr = window.devicePixelRatio || 1;
    const cw = stage.clientWidth, ch = stage.clientHeight;
    if (ov.width !== Math.round(cw * dpr) || ov.height !== Math.round(ch * dpr)) {
      ov.width = Math.round(cw * dpr); ov.height = Math.round(ch * dpr);
    }
    octx.setTransform(dpr, 0, 0, dpr, 0, 0);
    octx.clearRect(0, 0, cw, ch);
    const age = performance.now() - TRK.t;
    if (!TRK.target || age > FADE || !video.videoWidth || !S.mode) { TRK.target = TRK.shown = null; return; }
    // 부드러운 추적: 화면 표시 위치를 목표로 지수 보간
    const dt = lastDraw ? Math.min(100, now - lastDraw) : 16;
    lastDraw = now;
    const k = 1 - Math.exp(-dt / 55);
    // 캡처 시점 이후 경과 시간만큼 예측 (최대 250ms, 속도 감쇠)
    const lead = Math.min(250, performance.now() - TRK.capT);
    const dx = TRK.vel ? TRK.vel[0] * lead * 0.8 : 0, dy = TRK.vel ? TRK.vel[1] * lead * 0.8 : 0;
    const goal = TRK.target.map((p) => [p[0] + dx, p[1] + dy]);
    TRK.shown.forEach((p, i) => { p[0] += (goal[i][0] - p[0]) * k; p[1] += (goal[i][1] - p[1]) * k; });
    const { sc, ox, oy } = videoToScreen();
    const P = TRK.shown.map(([x, y]) => [ox + x * sc, oy + y * sc]);
    const alpha = age < 300 ? 1 : 1 - (age - 300) / (FADE - 300);
    const GREEN = '34,197,94', ORANGE = '245,158,11';
    const color = TRK.ok ? GREEN : ORANGE, label = TRK.ok ? '✓ 수신' : '인식 중…';
    octx.lineJoin = 'round'; octx.lineCap = 'round';
    // 반투명 채움 + 외곽선
    octx.beginPath();
    P.forEach(([x, y], i) => (i ? octx.lineTo(x, y) : octx.moveTo(x, y)));
    octx.closePath();
    octx.fillStyle = `rgba(${color},${0.12 * alpha})`;
    octx.fill();
    octx.strokeStyle = `rgba(${color},${0.9 * alpha})`;
    octx.lineWidth = 2;
    octx.stroke();
    // 2×2 다중 심볼: 칸 경계(가운데 십자선)를 얇게 표시
    const Hd = TRK.multi ? homography([[0, 0], [2, 0], [2, 2], [0, 2]], P) : null;
    if (Hd) {
      octx.lineWidth = 1;
      octx.beginPath();
      for (const [a, b] of [[[1, 0], [1, 2]], [[0, 1], [2, 1]]]) { octx.moveTo(...Hd(a)); octx.lineTo(...Hd(b)); }
      octx.stroke();
    }
    // 모서리 브래킷 (굵게)
    octx.strokeStyle = `rgba(${color},${0.9 * alpha})`;
    octx.lineWidth = 5;
    for (let i = 0; i < 4; i++) {
      const p = P[i], a = P[(i + 1) % 4], b = P[(i + 3) % 4];
      const L = 0.18;
      octx.beginPath();
      octx.moveTo(p[0] + (a[0] - p[0]) * L, p[1] + (a[1] - p[1]) * L);
      octx.lineTo(p[0], p[1]);
      octx.lineTo(p[0] + (b[0] - p[0]) * L, p[1] + (b[1] - p[1]) * L);
      octx.stroke();
    }
    // 라벨
    const top = P.reduce((m, p) => (p[1] < m[1] ? p : m), P[0]);
    octx.font = '600 13px -apple-system, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif';
    const tw = octx.measureText(label).width + 16;
    const lx = Math.max(4, Math.min(cw - tw - 4, top[0] - tw / 2)), ly = Math.max(4, top[1] - 30);
    octx.fillStyle = `rgba(${color},${0.95 * alpha})`;
    octx.beginPath();
    if (octx.roundRect) octx.roundRect(lx, ly, tw, 22, 11); else octx.rect(lx, ly, tw, 22);
    octx.fill();
    octx.fillStyle = `rgba(15,17,21,${alpha})`;
    octx.fillText(label, lx + 8, ly + 16);
    TRK.raf = requestAnimationFrame(drawTrack);
  }

  // ------------------------------------------------------------------ 파운틴 결과
  function onFountain(m) {
    if (m.type === 'progress') {
      if (m.isNew && S.cur && S.cur.session !== m.session) {
        setStatus('새 전송 감지 (세션 ' + hex(m.session) + ')');
        S.rankTimes = [];
      }
      const now = performance.now();
      if (m.useful) S.rankTimes.push(now);
      if (S.rankTimes.length > 120) S.rankTimes.shift();
      // 전송률 기준 시각: 이 세션의 첫 패킷을 인식한 시각 ~ 모든 블록을 받은 시각
      if (!S.rateT0.has(m.session)) S.rateT0.set(m.session, now);
      if (m.rank >= m.k && !S.rateT1.has(m.session)) S.rateT1.set(m.session, now);
      S.cur = m;
      renderProgress();
    } else if (m.type === 'assembling') {
      setStatus('모든 블록 수신 — 파일 복원 중…');
    } else if (m.type === 'file') {
      addFile(m);
    } else if (m.type === 'error') {
      setStatus(m.msg, true);
    } else if (m.type === 'dup-finished') {
      const nm = S.received.get(m.session);
      if (S.mode) setStatus(`✅ ${nm ? nm + ' ' : ''}수신 완료 — 같은 전송이 계속 보입니다. 송신측을 정지해도 됩니다.`);
    }
  }
  const hex = (n) => (n >>> 0).toString(16).toUpperCase().padStart(8, '0');

  /** 인식 시작 이후 평균 전송률 (bytes/s), 1초 미만이면 NaN */
  function rateOf(c) {
    const t0 = S.rateT0.get(c.session);
    if (t0 == null) return NaN;
    const t1 = S.rateT1.get(c.session);
    const sec = ((t1 || performance.now()) - t0) / 1000;
    return sec >= 1 || (t1 && sec > 0) ? Math.min(c.rank * c.bs, c.total) / sec : NaN;
  }
  const fmtRate = (bps) => {
    if (!isFinite(bps)) return '측정 중';
    if (bps >= 1024 * 1024) return (bps / 1048576).toFixed(2) + ' MB/s';
    const kb = bps / 1024;
    return kb.toFixed(kb < 10 ? 2 : 1) + ' KB/s';
  };

  function renderProgress() {
    const c = S.cur;
    if (!c) return;
    const pct = c.k ? (100 * c.rank / c.k) : 0;
    $('bar').style.width = pct.toFixed(1) + '%';
    $('stProg').textContent = pct.toFixed(pct < 10 ? 1 : 0) + '%';
    $('stRank').textContent = `${c.rank} / ${c.k}`;
    $('stPk').textContent = c.received + (c.useless ? ` (중복 ${c.useless})` : '');
    $('stSize').textContent = human(c.total);
    $('stSess').textContent = hex(c.session);
    $('stRate').textContent = fmtRate(rateOf(c));
    // 유효 패킷 수신 속도로 남은 시간 추정
    const t = S.rankTimes;
    let eta = NaN;
    if (t.length >= 3) {
      const rate = (t.length - 1) / ((t[t.length - 1] - t[0]) / 1000);
      eta = (c.k - c.rank) / rate;
    }
    $('stEta').textContent = c.rank >= c.k ? '완료' : fmtTime(eta);
    if (c.rank < c.k && S.mode) setStatus(`수신 중… ${c.rank}/${c.k} 블록`);
  }

  function addFile(m) {
    S.received.set(m.session, m.name);
    const blob = new Blob([m.data]);
    const url = URL.createObjectURL(blob);
    const el = document.createElement('div');
    el.className = 'file';
    const meta = document.createElement('div'); meta.className = 'meta';
    const nm = document.createElement('div'); nm.className = 'name'; nm.textContent = m.name;
    const info = document.createElement('div'); info.className = 'info';
    const c = S.cur && S.cur.session === m.session ? S.cur : null;
    const t0 = S.rateT0.get(m.session), t1 = S.rateT1.get(m.session);
    const took = t0 != null && t1 != null ? ` · ${fmtTime((t1 - t0) / 1000)}, 평균 ${fmtRate(c ? rateOf(c) : NaN)}` : '';
    info.textContent = `${human(m.data.byteLength)} · 프레임 ${m.frames}개${took} · ${new Date().toLocaleTimeString()}`;
    meta.append(nm, info);
    const a = document.createElement('a'); a.className = 'btn'; a.href = url; a.download = m.name; a.textContent = '💾 저장';
    el.append(meta, a);
    try {
      const f = new File([blob], m.name, { type: 'application/octet-stream' });
      if (navigator.canShare && navigator.canShare({ files: [f] })) {
        const b = document.createElement('button'); b.className = 'btn'; b.textContent = '공유';
        b.onclick = () => navigator.share({ files: [f], title: m.name }).catch(() => {});
        el.append(b);
      }
    } catch (e) { /* File 생성자 미지원 */ }
    $('files').prepend(el);
    $('bar').style.width = '100%';
    $('stProg').textContent = '100%';
    $('stEta').textContent = '완료';
    setStatus(`✅ 수신 완료: ${m.name} (${human(m.data.byteLength)}${c ? ', 평균 ' + fmtRate(rateOf(c)) : ''})`);
    if (navigator.vibrate) navigator.vibrate([60, 40, 60]);
    // 자동 다운로드 시도 (일부 모바일 브라우저는 사용자 탭이 필요 → 저장 버튼 제공)
    if (!/iPhone|iPad|iPod/i.test(navigator.userAgent)) { try { a.click(); } catch (e) { /* ignore */ } }
  }

  // ------------------------------------------------------------------ 프레임 캡처
  function guideFrac() { return +$('rngGuide').value / 100; }

  function layoutGuide() {
    const vw = video.videoWidth, vh = video.videoHeight;
    if (vw && vh) $('chipRes').textContent = `${vw}×${vh}`;
    // 100% = 전체 화면 분석 (가이드 상자 숨김, 추적 박스가 위치 피드백 역할)
    if (!vw || !vh || S.mode !== 'camera' || guideFrac() >= 1) { guide.style.display = 'none'; return; }
    const cw = stage.clientWidth, ch = stage.clientHeight;
    const sc = Math.min(cw / vw, ch / vh);
    const side = guideFrac() * Math.min(vw, vh) * sc;
    guide.style.display = 'block';
    guide.style.width = guide.style.height = side + 'px';
    guide.style.left = (cw - side) / 2 + 'px';
    guide.style.top = (ch - side) / 2 + 'px';
  }
  window.addEventListener('resize', layoutGuide);
  const guideLabel = () => { const v = +$('rngGuide').value; $('lblGuide').textContent = v >= 100 ? '전체 화면' : `가운데 ${v}%`; };
  $('rngGuide').oninput = () => { guideLabel(); layoutGuide(); };
  guideLabel();

  /** source 에서 (sx,sy,sw,sh) 영역을 잘라 최대 maxSide 로 축소한 RGBA 를 idle 워커에 보낸다 */
  /** source 의 (sx,sy,sw,sh) 영역을 최대 분석 크기로 축소해 RGBA 로 읽는다 → {img, dw, dh, sc} */
  function grabRegion(source, sx, sy, sw, sh) {
    const maxSide = +$('selMax').value;
    const sc = Math.min(1, maxSide / Math.max(sw, sh));
    const dw = Math.max(1, Math.round(sw * sc)), dh = Math.max(1, Math.round(sh * sc));
    if (cap.width !== dw || cap.height !== dh) { cap.width = dw; cap.height = dh; }
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(source, sx, sy, sw, sh, 0, 0, dw, dh);
    return { img: ctx.getImageData(0, 0, dw, dh), dw, dh, sc };
  }

  function newMeta(meta) {
    const id = ++S.lastId;
    S.frameMeta.set(id, meta);
    if (S.frameMeta.size > 64) S.frameMeta.delete(S.frameMeta.keys().next().value);
    return id;
  }

  function sendRegion(source, sx, sy, sw, sh, kind) {
    const x = idleWorker();
    if (!x) return false;
    const g = grabRegion(source, sx, sy, sw, sh);
    x.busy = true;
    const id = newMeta({ sx, sy, sc: g.sc, t: performance.now(), video: source === video, kind: kind || 'full' });
    x.w.postMessage({ type: 'frame', id, w: g.dw, h: g.dh, buf: g.img.data.buffer }, [g.img.data.buffer]);
    return true;
  }
  S.lastId = 0;
  S.frameMeta = new Map();

  /** 다음 분석 작업 하나를 보낸다 (ROI 추적 중이면 ROI, 아니면 탐색 영역) */
  function dispatch(source, vw, vh) {
    const r = chooseRegion(vw, vh);
    return sendRegion(source, r.sx, r.sy, r.sw, r.sh, r.kind);
  }

  function pump() {
    if (S.mode !== 'camera') return;
    const vw = video.videoWidth, vh = video.videoHeight;
    if (!vw || !vh || video.readyState < 2) return;
    while (idleWorker()) {
      // 같은 비디오 프레임을 두 번 분석하지 않음 (rVFC 미지원 시 시간 기반)
      if (S.rvfc) { if (S.frameSeq <= S.lastSent) return; S.lastSent = S.frameSeq; }
      else {
        const t = performance.now();
        if (S.lastSentT && t - S.lastSentT < 30) { setTimeout(pump, 30); return; }
        S.lastSentT = t;
      }
      if (!dispatch(video, vw, vh)) return;
    }
  }

  function onVideoFrame() {
    if (S.mode !== 'camera') return;
    S.frameSeq++;
    pump();
    video.requestVideoFrameCallback(onVideoFrame);
  }

  // ------------------------------------------------------------------ HUD
  setInterval(() => {
    const now = performance.now();
    while (S.analyzed.length && now - S.analyzed[0] > 2000) S.analyzed.shift();
    $('chipFps').textContent = `분석 ${(S.analyzed.length / 2).toFixed(1)} fps`;
    const h = S.hits.length ? (100 * S.hits.reduce((a, b) => a + b, 0) / S.hits.length) : 0;
    $('chipHit').textContent = `인식 ${h.toFixed(0)}%`;
    $('chipRoi').textContent = (ROI.c && ROI.last ? `추적 ${ROI.last.sw}×${ROI.last.sh}` : '탐색 중') + (S.multi ? ' · 2×2' : '');
    if (S.cur) renderProgress();
  }, 500);

  // ------------------------------------------------------------------ 카메라
  async function listCameras() {
    try {
      const devs = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput');
      const sel = $('selCam'), cur = sel.value;
      sel.innerHTML = '<option value="">후면 카메라 (기본)</option>';
      devs.forEach((d, i) => {
        const o = document.createElement('option');
        o.value = d.deviceId; o.textContent = d.label || `카메라 ${i + 1}`;
        sel.appendChild(o);
      });
      sel.value = cur;
    } catch (e) { /* ignore */ }
  }

  async function startCamera() {
    stopAll();
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
      setStatus('이 브라우저는 카메라 API를 지원하지 않습니다. 동영상/사진 모드를 사용하세요.', true);
      return;
    }
    const [w, h] = $('selRes').value.split('x').map(Number);
    const devId = $('selCam').value;
    const v = { width: { ideal: w }, height: { ideal: h }, frameRate: { ideal: 30 } };
    if (devId) v.deviceId = { exact: devId }; else v.facingMode = { ideal: 'environment' };
    setStatus('카메라 권한 요청 중…');
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: v, audio: false });
    } catch (e) {
      try { stream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false }); }
      catch (e2) {
        const n = e2.name || e.name;
        const msg = n === 'NotAllowedError' ? '카메라 권한이 거부되었습니다. 브라우저 설정에서 카메라 권한을 허용하세요.'
          : n === 'NotFoundError' ? '카메라를 찾을 수 없습니다.'
          : n === 'NotReadableError' ? '다른 앱이 카메라를 사용 중입니다.'
          : '카메라를 열 수 없습니다: ' + (e2.message || n);
        setStatus(msg, true);
        return;
      }
    }
    S.stream = stream;
    S.track = stream.getVideoTracks()[0];
    video.removeAttribute('src');
    video.srcObject = stream;
    video.setAttribute('playsinline', ''); video.muted = true;
    try { await video.play(); } catch (e) { /* iOS: autoplay 정책 — 이미 사용자 탭이므로 보통 OK */ }
    S.mode = 'camera';
    S.frameSeq = 0; S.lastSent = -1;
    $('ph').style.display = 'none';
    $('hud').style.display = 'flex';
    $('btnStop').disabled = false;
    $('btnStart').textContent = '🔄 카메라 다시 시작';
    setupTrackControls();
    listCameras();
    requestWakeLock();
    await new Promise((r) => (video.videoWidth ? r() : video.addEventListener('loadedmetadata', r, { once: true })));
    layoutGuide();
    if (S.rvfc) video.requestVideoFrameCallback(onVideoFrame);
    else (function loop() { if (S.mode === 'camera') { pump(); requestAnimationFrame(loop); } })();
    setStatus(S.cur && S.cur.rank < S.cur.k ? '수신 재개' : 'JAB Code를 카메라에 비추세요');
  }

  function setupTrackControls() {
    const t = S.track;
    $('rowZoom').style.display = 'none';
    if (!t || !t.getCapabilities) return;
    const caps = t.getCapabilities();
    try { if (caps.focusMode && caps.focusMode.includes('continuous')) t.applyConstraints({ advanced: [{ focusMode: 'continuous' }] }); } catch (e) {}
    if (caps.zoom) {
      const z = $('rngZoom');
      z.min = caps.zoom.min; z.max = Math.min(caps.zoom.max, 8); z.step = caps.zoom.step || 0.1;
      const cur = (t.getSettings && t.getSettings().zoom) || caps.zoom.min;
      z.value = cur; $('lblZoom').textContent = (+cur).toFixed(1) + '×';
      $('rowZoom').style.display = 'flex';
      z.oninput = () => {
        $('lblZoom').textContent = (+z.value).toFixed(1) + '×';
        t.applyConstraints({ advanced: [{ zoom: +z.value }] }).catch(() => {});
      };
    }
  }

  async function requestWakeLock() {
    try { if ('wakeLock' in navigator) S.wakeLock = await navigator.wakeLock.request('screen'); } catch (e) {}
  }
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && S.mode === 'camera') requestWakeLock();
  });

  function stopAll() {
    S.mode = null;
    if (S.stream) S.stream.getTracks().forEach((t) => t.stop());
    S.stream = null; S.track = null;
    video.srcObject = null;
    if (S.fileUrl) { URL.revokeObjectURL(S.fileUrl); S.fileUrl = null; }
    video.removeAttribute('src');
    guide.style.display = 'none';
    TRK.target = TRK.shown = TRK.vel = null; TRK.applied = 0; TRK.rej = 0;
    roiReset();
    S.multi = true; ROI.nodata = 0;
    octx.clearRect(0, 0, ov.width, ov.height);
    $('hud').style.display = 'none';
    $('ph').style.display = 'flex';
    $('btnStop').disabled = true;
    $('btnStart').textContent = '📷 카메라로 수신 시작';
    if (S.wakeLock) { S.wakeLock.release().catch(() => {}); S.wakeLock = null; }
  }

  // ------------------------------------------------------------------ 동영상/사진 파일 모드
  function waitWorker() {
    return new Promise((r) => { if (idleWorker()) r(); else S.fileWaiter = r; });
  }
  async function waitAllWorkers() {
    while (S.workers.some((x) => x.busy)) await new Promise((r) => setTimeout(r, 50));
  }

  async function processFiles(files) {
    stopAll();
    S.mode = 'file';
    $('btnStop').disabled = false;
    $('ph').style.display = 'none';
    $('hud').style.display = 'flex';
    for (const f of files) {
      if (S.mode !== 'file') break;
      if (f.type.startsWith('image/')) await processImage(f);
      else await processVideo(f);
    }
    await waitAllWorkers();
    if (S.mode === 'file') {
      setStatus(S.cur && S.cur.rank >= S.cur.k ? '파일 분석 완료' : '파일 분석 끝 — 블록이 부족합니다. 더 긴 동영상을 촬영해 보세요.');
      stopAll();
    }
  }

  async function processImage(f) {
    let src;
    try { src = await createImageBitmap(f); }
    catch (e) {
      src = await new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = URL.createObjectURL(f); });
    }
    const w = src.width || src.naturalWidth, h = src.height || src.naturalHeight;
    await waitWorker();
    sendRegion(src, 0, 0, w, h);
    setStatus(`사진 분석: ${f.name}`);
  }

  async function processVideo(f) {
    S.fileUrl = URL.createObjectURL(f);
    video.srcObject = null;
    video.src = S.fileUrl;
    video.muted = true;
    await new Promise((r, j) => { video.onloadedmetadata = r; video.onerror = () => j(new Error('동영상을 열 수 없습니다')); })
      .catch((e) => { setStatus(e.message, true); });
    if (!video.duration) return;
    try { await video.play(); video.pause(); } catch (e) {}
    const dur = video.duration, step = 1 / 12;   // 송신 6fps 기준 코드 프레임당 2회 샘플
    for (let t = 0; t < dur && S.mode === 'file'; t += step) {
      if (S.cur && S.cur.rank >= S.cur.k && S.cur.k) break;
      await waitWorker();
      video.currentTime = t;
      await new Promise((r) => { const done = () => r(); video.addEventListener('seeked', done, { once: true }); setTimeout(done, 1500); });
      dispatch(video, video.videoWidth, video.videoHeight);
      setStatus(`동영상 분석 중… ${Math.round(100 * t / dur)}% (${f.name})`);
    }
  }

  // ------------------------------------------------------------------ 버튼
  $('btnStart').onclick = () => startCamera();
  $('btnStop').onclick = () => { stopAll(); setStatus('정지됨'); };
  $('selCam').onchange = () => { if (S.mode === 'camera') startCamera(); };
  $('selRes').onchange = () => { if (S.mode === 'camera') startCamera(); };
  $('fileInput').onchange = (e) => { const fs = [...e.target.files]; e.target.value = ''; if (fs.length) processFiles(fs); };
  $('btnReset').onclick = () => {
    fountain.postMessage({ type: 'reset' });
    S.cur = null; S.rankTimes = [];
    S.rateT0.clear(); S.rateT1.clear();
    $('bar').style.width = '0%';
    ['stProg', 'stRank', 'stPk', 'stEta', 'stRate', 'stSize', 'stSess'].forEach((id) => ($(id).textContent = '-'));
    setStatus('수신 상태를 초기화했습니다');
  };
  video.addEventListener('resize', layoutGuide);

  // 테스트 훅
  window.__jab = S;
  window.__jabDbg = { ROI, TRK };   // 테스트/디버그용 내부 상태
})();
