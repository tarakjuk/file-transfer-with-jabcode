# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 개요

JAB Code(컬러 2D 코드) 기반 단방향 광학 파일 전송기. PC 화면에 JAB Code 프레임을 연속 표시하고, 스마트폰 브라우저가 카메라로 읽어 파일을 복원한다.

- `sender/` — Python + PySide6 송신 GUI. 제공된 `libjabcode.dll`(ctypes)로 프레임 생성, 실패 시 `jabcodeWriter.exe` 폴백.
- `receiver/` — 정적 웹앱(빌드 단계 없음, 폴더 그대로 VibeDrop 배포). `jabcode.wasm`(libjabcode v2.0.0 디코더) + Web Worker.
- `wasm/` — libjabcode → WASM 빌드 소스.
- `tools/` — 카메라 왜곡 시뮬레이션·헤드리스 브라우저 종단 테스트.

**`인수인계.txt` 가 상세 설계 문서이자 작업 이력이다** (프로토콜 바이트 레이아웃, 설계 근거, 측정치, 미해결 과제). 큰 변경 전에 해당 장을 읽을 것. 사용자는 한국어로 소통하며 간결한 답변을 선호한다.

## 명령어

```bat
run_sender.bat              :: .venv 생성 → requirements 변경 시에만 설치 → sender\jab_sender.py 실행
deploy_receiver.bat         :: npx @vibedrop/cli 로 receiver\ 배포 (Node.js 필요), receiver_slug.txt 의 slug 로 같은 주소 갱신
```

venv 직접 사용: `.venv\Scripts\python.exe sender\jab_sender.py` (Windows spawn 이라 `__main__` 가드/`freeze_support` 유지 필수). 헤드리스 확인은 `QT_QPA_PLATFORM=offscreen`.

정식 테스트 프레임워크는 없다. 검증은 스크립트로 한다:

```
python sender/export_frames.py <파일> frames/ [--count N --version 8 --ecc 5 --scale 6]   # 단일 심볼 프레임 PNG 덤프 (2×2 미지원)
python tools/degrade.py frames/ cam/ "frac=0.6,blur=1.0,tilt=0.06,noise=5"              # 카메라 촬영 시뮬레이션
node tools/nodetest.js cam/ out/                                                          # WASM + 파운틴 복원 (tools/ 에서 npm i)

# Windows 종단 회귀 테스트 (pip install numpy pillow playwright, 설치된 Edge 사용)
cd receiver && python -m http.server 8765
python tools/make_moving_y4m.py C:\tmp\small.y4m <파일> 0.28 [프레임수 병렬(1|4) 송신fps 카메라fps]
python tools/e2e_edge.py C:\tmp\small.y4m <파일> 120 http://127.0.0.1:8765/ <태그>
```

WASM 재빌드: `WASI_SDK=... JABCODE_SRC=.../jabcode/src/jabcode ./wasm/build.sh` (wasi-sdk 25, `image.c` 제외, 출력은 `receiver/jabcode.wasm`).

## 아키텍처

```
송신: 파일 → 컨테이너(JFC1: 파일명·CRC·zlib) → 파운틴 패킷(28B 헤더 "JF") → libjabcode → QTimer 로 N fps 표시
수신: 카메라 → ROI 크롭 → decode-worker×N (WASM) → fountain-worker (CRC·GF(2) 가우스 소거·압축 해제) → 다운로드
```

### 송신 ↔ 수신 프로토콜 동기화 (가장 중요)
`sender/jabfountain.py` 와 `receiver/jabcore.js` 는 **비트 단위로 일치**해야 한다: 컨테이너/패킷 레이아웃, `mulberry32`(Python 은 `& 0xFFFFFFFF` 마스킹), `block_indices`, CRC 범위. 프로토콜을 바꾸면 두 파일을 함께 수정하고 `PROTO_VERSION` 을 올리며, 수신기를 먼저 배포한다.

- 파운틴 코드는 **비시스테매틱 + 균등 degree + 온라인 가우스 소거**. degree 는 반드시 홀/짝 혼합(짝수만이면 GF(2) rank 가 K-1 에서 막힘). 피일링 디코더/Robust Soliton 은 시뮬레이션에서 열세라 버린 설계다.
- 세션 ID = 컨테이너 CRC ^ 설정 서명 → 같은 파일·설정이면 송신 재시작 후에도 수신 진행이 이어진다. 2×2 다중 심볼은 서명에 `/m4` 추가.
- 패킷 flags bit2-3 = 배치 (0 단일, 1 JAB 2×2 다중 심볼). 프레임당 패킷 1개.

### 송신 (`sender/`)
- `jabcode_backend.py`: ctypes 구조체 오프셋은 jabcode.h v2.0.0 기준(`jab_encode` 크기 72). 주의점:
  - `generateJABCode` 는 **성공 시 0** 반환 (JAB_SUCCESS=1 아님).
  - libjabcode 는 전역 PRNG 상태가 있어 스레드 안전하지 않음 → 프레임 생성은 `framegen.py` 의 **ProcessPoolExecutor** 로만.
  - 매 프레임 `createEncode`/`destroyEncode` (enc 재사용 금지).
  - 색 수는 `COLOR_NUMBER = 8` 고정 (제공 DLL 은 16색에서 항상 access violation).
  - 다중 심볼 위치 `MULTI_2X2 = (0, 4, 2, 10)`. EXE 폴백은 `--symbol-number` 를 다른 인자보다 먼저 줘야 함.
  - DLL/EXE 검색: `JABCODE_DIR` 환경변수 → sender → 상위 폴더들 → cwd.
- `jab_sender.py`: GUI, `Producer(QThread)` 가 프로세스 풀 결과를 deque 에 버퍼링, `QTimer(PreciseTimer)` 로 표시. 설정 변경 시 용량 재측정 후 재시작. 수신 URL 은 `receiver_url.txt` 에서 읽어 QR 로 표시.

### 수신 (`receiver/`)
- `jabcore.js`: 공용 로직 UMD (브라우저 워커·메인 스레드·Node `tools/nodetest.js` 겸용). 워커 안에서 `module` 이라는 변수명을 쓰면 UMD 판별과 충돌한다.
- `decode-worker.js`: WASM 인스턴스, 메모리 초과/trap 시 인스턴스 재생성.
- `fountain-worker.js`: 세션별 디코더, 압축 해제는 `DecompressionStream('deflate')` → 실패 시 `fflate`.
- `app.js`: 카메라·프레임 분배·ROI 추적·추적 박스(마스터 파인더 패턴 4점 → 호모그래피, 등속 예측)·UI. 첫 디코드 전에는 다중 심볼로 가정해 ROI 를 넓게 잡는다(단일로 자르면 슬레이브가 잘려 ROI 가 고정되는 함정). 디버그 훅 `window.__jab`, `window.__jabDbg`.
- WASM export(`wasm/jabwasm.c`): `jw_buffer`, `jw_decode`, `jw_result`, `jw_status`, `jw_geom` (geom 은 디코드 실패해도 파인더 패턴 검출 시 채워짐; 검출만 된 경우 side 값은 신뢰 불가 → app.js 가 캐시/추정으로 보정).
- HTTPS 필수(카메라 정책).

## 작업 규칙

- `receiver/` 수정 후: VibeDrop 재배포(`deploy_receiver.bat`, 현재 slug `bk76xykb`) + 가능하면 `tools/e2e_edge.py` 회귀 테스트.
- `jabcodeReader.exe`, `jabcodeWriter.exe`, `libjabcode.dll` 은 사용자 제공 원본 — 수정하지 않는다.
- 기본 설정(2×2 다중 심볼, v8, ECC5, 5fps)은 사용자가 정한 것. 성능상 다른 설정이 낫더라도(인수인계 17장) 기본값 변경은 사용자 확인 후.
- 실제 휴대폰(특히 iPhone Safari) 테스트는 아직 미검증 상태다.
