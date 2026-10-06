"""
JAB Code 광학 파일 송신기 (PySide6 GUI)

  python jab_sender.py            # GUI 실행
  python jab_sender.py 파일경로    # 파일을 미리 선택한 상태로 실행

필요: PySide6 (필수), qrcode·pillow (선택: 수신 페이지 QR / EXE 폴백)
libjabcode.dll 또는 jabcodeWriter.exe 가 이 파일과 같은 폴더나 상위 폴더에 있어야 한다.
"""
from __future__ import annotations

import collections
import multiprocessing as mp
import os
import random
import sys
import threading
import time
import zlib
from concurrent.futures import TimeoutError as FutTimeout

from PySide6.QtCore import QRect, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

import framegen
from jabcode_backend import COLOR_NUMBER, MULTI_2X2, SINGLE, JabCodeError, load_backend, probe_capacity
from jabfountain import HEADER_SIZE, LAYOUT_MULTI2, LAYOUT_SINGLE, FountainEncoder, build_container, packet_flags

APP_NAME = "JAB 광학 파일 송신기"
DEFAULT_RECEIVER_URL = ""   # 비어 있으면 ../receiver_url.txt (deploy_receiver 스크립트가 생성) 를 사용


def receiver_url_from_file() -> str:
    """deploy_receiver.bat 이 기록한 수신 페이지 주소를 읽는다."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here)):
        p = os.path.join(d, "receiver_url.txt")
        try:
            with open(p, encoding="utf-8-sig") as f:
                u = f.read().strip()
            if u.startswith("http"):
                return u
        except OSError:
            pass
    return ""
QUIET_MODULES = 4  # 심볼 주변 흰 여백 (모듈 단위)


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def fmt_time(sec: float) -> str:
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}초"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}분 {s}초"
    h, m = divmod(m, 60)
    return f"{h}시간 {m}분"


# =================================================================== 코드 표시 위젯


class CodeView(QWidget):
    doubleClicked = Signal()

    def __init__(self):
        super().__init__()
        self.img: QImage | None = None
        self.message = "파일을 선택하거나 이 영역에 끌어다 놓으세요"
        self.setMinimumSize(320, 320)
        self.setAutoFillBackground(False)

    def set_image(self, img: QImage | None):
        self.img = img
        self.update()

    def set_message(self, text: str):
        self.message = text
        self.update()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        if self.img is None:
            p.setPen(QColor(120, 120, 120))
            f = QFont()
            f.setPointSize(13)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        w, h = self.img.width(), self.img.height()
        avail_w, avail_h = self.width(), self.height()
        # 모듈 하나가 정수 픽셀이 되도록 배율 결정 (최근접 보간 → 선명한 경계)
        scale = max(1, min(avail_w // (w + 2 * QUIET_MODULES), avail_h // (h + 2 * QUIET_MODULES)))
        tw, th = w * scale, h * scale
        x, y = (avail_w - tw) // 2, (avail_h - th) // 2
        p.setRenderHint(QPainter.SmoothPixmapTransform, False)
        p.drawImage(QRect(x, y, tw, th), self.img)


# =================================================================== 프레임 생산 스레드


class Producer(QThread):
    error = Signal(str)

    def __init__(self, cfg: dict, start_seed: int, capacity: int):
        super().__init__()
        self.cfg = cfg
        self.seed = start_seed
        self.capacity = capacity
        self.frames: collections.deque = collections.deque()
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self.generated = 0
        self.gen_time = 0.0

    def stop(self):
        self._stop.set()

    def pop(self):
        with self.lock:
            return self.frames.popleft() if self.frames else None

    def buffered(self):
        with self.lock:
            return len(self.frames)

    def run(self):
        c = self.cfg
        try:
            pool = framegen.make_pool(c["prefer"], c["container"], c["block_size"], c["session"],
                                      c["colors"], c["version"], c["ecc"], c["positions"], c["flags"], c.get("workers"))
        except Exception as ex:  # pragma: no cover
            self.error.emit(f"워커 시작 실패: {ex}")
            return
        pending: collections.deque = collections.deque()
        nworkers = pool._max_workers
        try:
            while not self._stop.is_set():
                # 버퍼 + 진행중 작업이 capacity 보다 적으면 더 제출
                while len(pending) < nworkers * 2 and self.buffered() + len(pending) < self.capacity:
                    pending.append((time.perf_counter(), pool.submit(framegen.make_frame, self.seed & 0xFFFFFFFF)))
                    self.seed += 1
                if not pending:
                    time.sleep(0.005)
                    continue
                t0, fut = pending[0]
                try:
                    seed, deg, w, h, rgba = fut.result(timeout=0.2)
                except (FutTimeout, TimeoutError):
                    continue
                except JabCodeError:
                    pending.popleft()
                    continue  # 이 프레임만 건너뜀
                pending.popleft()
                img = QImage(rgba, w, h, w * 4, QImage.Format_RGBA8888).copy()
                with self.lock:
                    self.frames.append((seed, img))
                self.generated += 1
        except Exception as ex:
            self.error.emit(f"프레임 생성 오류: {ex}")
        finally:
            for _, f in pending:
                f.cancel()
            pool.shutdown(wait=False, cancel_futures=True)


# =================================================================== QR 다이얼로그


class QrDialog(QDialog):
    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("수신 페이지 열기")
        lay = QVBoxLayout(self)
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignCenter)
        try:
            import qrcode
            qr = qrcode.QRCode(border=3, box_size=8)
            qr.add_data(url)
            qr.make(fit=True)
            pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            data = pil.tobytes("raw", "RGB")
            qimg = QImage(data, pil.width, pil.height, pil.width * 3, QImage.Format_RGB888).copy()
            lbl.setPixmap(QPixmap.fromImage(qimg))
        except Exception:
            lbl.setText("(qrcode 패키지가 없어 QR을 표시할 수 없습니다: pip install qrcode pillow)")
        lay.addWidget(lbl)
        t = QLabel(f"휴대폰 카메라로 QR을 찍어 수신 페이지를 여세요\n{url}")
        t.setAlignment(Qt.AlignCenter)
        t.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(t)


# =================================================================== 메인 윈도우


class MainWindow(QMainWindow):
    def __init__(self, initial_file: str | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self.settings = QSettings("jabtx", "sender")

        try:
            self.backend = load_backend(self.settings.value("prefer", None))
        except JabCodeError as ex:
            QMessageBox.critical(self, APP_NAME, str(ex))
            raise SystemExit(1)

        self.file_path: str | None = None
        self.file_data: bytes | None = None
        self.producer: Producer | None = None
        self.cfg: dict | None = None
        self.shown = 0
        self.underruns = 0
        self.fps_hist: collections.deque = collections.deque(maxlen=60)
        self.dirty = True

        # ---- 좌측 설정 패널
        side = QWidget()
        side.setFixedWidth(330)
        sl = QVBoxLayout(side)

        gfile = QGroupBox("파일")
        fl = QVBoxLayout(gfile)
        self.btn_open = QPushButton("파일 선택…")
        self.btn_open.clicked.connect(self.choose_file)
        self.lbl_file = QLabel("선택된 파일 없음")
        self.lbl_file.setWordWrap(True)
        fl.addWidget(self.btn_open)
        fl.addWidget(self.lbl_file)
        sl.addWidget(gfile)

        gcode = QGroupBox("JAB Code 설정")
        cf = QFormLayout(gcode)
        self.cmb_grid = QComboBox()
        self.cmb_grid.addItem("4개 — JAB 다중 심볼 2×2 (권장)", 4)
        self.cmb_grid.addItem("1개", 1)
        self.cmb_grid.setToolTip("JAB Code 다중 심볼: 마스터 1개 + 슬레이브 3개를 간격 없이 붙인 하나의 코드.\n"
                                 "패킷 1개를 4칸에 나눠 담아 프레임당 데이터 약 4배. 한 칸이라도 못 읽으면 그 프레임은 버려짐")
        self.spin_ver = QSpinBox()
        self.spin_ver.setRange(1, 32)
        self.spin_ver.setValue(8)
        self.spin_ver.setToolTip("심볼 버전: 한 변 = 4×버전+17 모듈. 클수록 용량↑, 인식 난이도↑")
        self.spin_ecc = QSpinBox()
        self.spin_ecc.setRange(1, 10)
        self.spin_ecc.setValue(5)
        self.spin_ecc.setToolTip("오류정정 레벨 (높을수록 견고, 용량↓)")
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(1, 60)
        self.spin_fps.setValue(5)
        self.spin_fps.setSuffix(" fps")
        self.chk_zip = QCheckBox("zlib 압축 (효과 있을 때만)")
        self.chk_zip.setChecked(True)
        self.cmb_backend = QComboBox()
        self.cmb_backend.addItem("libjabcode.dll (빠름)", "dll")
        self.cmb_backend.addItem("jabcodeWriter.exe (폴백)", "exe")
        if self.backend.name.startswith("jabcodeWriter"):
            self.cmb_backend.setCurrentIndex(1)
        cf.addRow("병렬 심볼", self.cmb_grid)
        cf.addRow("심볼 버전", self.spin_ver)
        cf.addRow("ECC 레벨", self.spin_ecc)
        cf.addRow("프레임 속도", self.spin_fps)
        cf.addRow("", self.chk_zip)
        cf.addRow("생성 엔진", self.cmb_backend)
        sl.addWidget(gcode)

        for w in (self.cmb_grid, self.spin_ver, self.spin_ecc, self.chk_zip, self.cmb_backend):
            sig = getattr(w, "currentIndexChanged", None) or getattr(w, "valueChanged", None) or w.toggled
            sig.connect(self.mark_dirty)
        self.spin_fps.valueChanged.connect(self.apply_fps)

        gctl = QGroupBox("전송")
        cl = QVBoxLayout(gctl)
        row = QHBoxLayout()
        self.btn_start = QPushButton("▶ 송신 시작")
        self.btn_start.setStyleSheet("font-weight:bold; padding:8px;")
        self.btn_start.clicked.connect(self.toggle)
        self.btn_stop = QPushButton("■ 정지")
        self.btn_stop.clicked.connect(self.stop)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        cl.addLayout(row)
        self.btn_full = QPushButton("전체 화면 (F11 / 더블클릭)")
        self.btn_full.clicked.connect(self.toggle_full)
        cl.addWidget(self.btn_full)
        sl.addWidget(gctl)

        grecv = QGroupBox("수신 페이지")
        rl = QVBoxLayout(grecv)
        self.ed_url = QLineEdit(receiver_url_from_file() or self.settings.value("receiver_url", DEFAULT_RECEIVER_URL) or "")
        self.ed_url.setPlaceholderText("배포된 수신 페이지 주소 (https://….vibedrop.site)")
        self.ed_url.editingFinished.connect(lambda: self.settings.setValue("receiver_url", self.ed_url.text()))
        btn_qr = QPushButton("휴대폰용 QR 보기")
        btn_qr.clicked.connect(self.show_qr)
        rl.addWidget(self.ed_url)
        rl.addWidget(btn_qr)
        sl.addWidget(grecv)

        self.lbl_info = QLabel()
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setTextFormat(Qt.RichText)
        self.lbl_info.setStyleSheet("background:#f4f5f7; border:1px solid #ddd; padding:8px; border-radius:4px;")
        sl.addWidget(self.lbl_info)
        sl.addStretch(1)
        self.lbl_engine = QLabel(f"엔진: {self.backend.name}")
        self.lbl_engine.setStyleSheet("color:#777; font-size:11px;")
        sl.addWidget(self.lbl_engine)
        self.side = side

        # ---- 우측 코드 뷰
        self.view = CodeView()
        self.view.doubleClicked.connect(self.toggle_full)

        central = QWidget()
        hl = QHBoxLayout(central)
        hl.setContentsMargins(6, 6, 6, 6)
        hl.addWidget(side)
        hl.addWidget(self.view, 1)
        self.setCentralWidget(central)

        act_full = QAction(self)
        act_full.setShortcut(QKeySequence(Qt.Key_F11))
        act_full.triggered.connect(self.toggle_full)
        self.addAction(act_full)
        act_esc = QAction(self)
        act_esc.setShortcut(QKeySequence(Qt.Key_Escape))
        act_esc.triggered.connect(lambda: self.toggle_full() if self.isFullScreen() else None)
        self.addAction(act_esc)
        act_space = QAction(self)
        act_space.setShortcut(QKeySequence(Qt.Key_Space))
        act_space.triggered.connect(self.toggle)
        self.addAction(act_space)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.stat_timer = QTimer(self)
        self.stat_timer.timeout.connect(self.update_info)
        self.stat_timer.start(500)

        self.update_info()
        if initial_file:
            self.load_file(initial_file)

    def show_qr(self):
        url = self.ed_url.text().strip()
        if not url:
            QMessageBox.information(self, APP_NAME, "수신 페이지 주소를 먼저 입력하세요.\n(deploy_receiver.bat 실행 시 자동으로 채워집니다)")
            return
        self.settings.setValue("receiver_url", url)
        QrDialog(url, self).exec()

    # ------------------------------------------------------------- 파일
    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "보낼 파일 선택", self.settings.value("last_dir", ""))
        if path:
            self.load_file(path)

    def load_file(self, path: str):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as ex:
            QMessageBox.warning(self, APP_NAME, f"파일을 읽을 수 없습니다:\n{ex}")
            return
        if len(data) > 0xFFFFFFF0:
            QMessageBox.warning(self, APP_NAME, "4GB 이상 파일은 지원하지 않습니다.")
            return
        self.settings.setValue("last_dir", os.path.dirname(path))
        self.stop()
        self.file_path, self.file_data = path, data
        self.lbl_file.setText(f"<b>{os.path.basename(path)}</b><br>{human(len(data))}")
        self.view.set_message("▶ 송신 시작을 누르세요")
        self.mark_dirty()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if urls and os.path.isfile(urls[0]):
            self.load_file(urls[0])

    # ------------------------------------------------------------- 설정/준비
    def mark_dirty(self, *_):
        was_running = self.producer is not None and self.timer.isActive()
        self.dirty = True
        self.cfg = None
        if self.producer:
            self.stop()
            if was_running:
                self.start()
        self.update_info()

    def apply_fps(self, *_):
        if self.timer.isActive():
            self.timer.start(int(round(1000 / self.spin_fps.value())))
        self.update_info()

    def prepare(self) -> bool:
        if self.file_data is None:
            QMessageBox.information(self, APP_NAME, "먼저 보낼 파일을 선택하세요.")
            return False
        prefer = self.cmb_backend.currentData()
        want_exe = prefer == "exe"
        if want_exe != self.backend.name.startswith("jabcodeWriter"):
            try:
                self.backend = load_backend(prefer)
            except JabCodeError as ex:
                QMessageBox.warning(self, APP_NAME, str(ex))
                return False
            self.settings.setValue("prefer", prefer)
            self.lbl_engine.setText(f"엔진: {self.backend.name}")
        colors = COLOR_NUMBER
        version, ecc = self.spin_ver.value(), self.spin_ecc.value()
        grid = self.cmb_grid.currentData()
        positions = MULTI_2X2 if grid == 4 else SINGLE
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            cap = probe_capacity(self.backend, colors, version, ecc, positions)
        finally:
            QApplication.restoreOverrideCursor()
        block = ((cap - HEADER_SIZE) // 4) * 4
        if block < 16:
            QMessageBox.warning(self, APP_NAME, f"이 설정의 심볼 용량({cap} B)이 너무 작습니다. 버전을 올리거나 ECC를 낮추세요.")
            return False
        container = build_container(self.file_path, self.file_data, self.chk_zip.isChecked())
        settings_sig = f"{colors}/{version}/{ecc}/{block}{'/m4' if grid == 4 else ''}".encode()
        session = (zlib.crc32(container) ^ (zlib.crc32(settings_sig) << 1)) & 0xFFFFFFFF or 1
        enc = FountainEncoder(container, block, session)
        self.cfg = dict(prefer=prefer, container=container, block_size=block, session=session,
                        colors=colors, version=version, ecc=ecc, k=enc.k, capacity=cap, grid=grid,
                        positions=positions, flags=packet_flags(LAYOUT_MULTI2 if grid == 4 else LAYOUT_SINGLE))
        self.dirty = False
        return True

    # ------------------------------------------------------------- 송신 제어
    def toggle(self):
        if self.timer.isActive():
            self.pause()
        else:
            self.start()

    def start(self):
        if self.cfg is None or self.dirty:
            if not self.prepare():
                return
        if self.producer is None:
            buf = max(8, self.spin_fps.value() * 2)
            # 같은 파일·설정이면 세션 ID가 같으므로, 다시 시작해도 수신측 진행이 유지된다.
            self.producer = Producer(self.cfg, random.getrandbits(31), buf)
            self.producer.error.connect(self.on_error)
            self.producer.start()
            self.shown = 0
            self.underruns = 0
            self.view.set_message("프레임 생성 중…")
        self.timer.start(int(round(1000 / self.spin_fps.value())))
        self.btn_start.setText("❚❚ 일시정지")

    def pause(self):
        self.timer.stop()
        self.btn_start.setText("▶ 송신 재개")

    def stop(self):
        self.timer.stop()
        if self.producer:
            self.producer.stop()
            self.producer.wait(3000)
            self.producer = None
        self.btn_start.setText("▶ 송신 시작")
        self.view.set_image(None)
        if self.file_data is not None:
            self.view.set_message("정지됨 — ▶ 송신 시작을 누르세요")

    def on_error(self, msg):
        self.stop()
        QMessageBox.critical(self, APP_NAME, msg)

    def tick(self):
        if not self.producer:
            return
        fr = self.producer.pop()
        if fr is None:
            if self.shown:   # 첫 프레임이 나오기 전(워커 시작 대기)은 지연으로 세지 않음
                self.underruns += 1
            return
        _, img = fr
        self.view.set_image(img)
        self.shown += 1
        self.fps_hist.append(time.perf_counter())

    # ------------------------------------------------------------- 정보 표시
    def update_info(self):
        fps = self.spin_fps.value()
        lines = []
        if self.cfg:
            c = self.cfg
            k = c["k"]
            lines.append(f"세션 ID: <b>{c['session']:08X}</b>")
            side = 4 * c['version'] + 17
            lines.append(f"심볼: {side}×{side} 모듈, {c['colors']}색, ECC {c['ecc']}")
            if c["grid"] == 4:
                lines.append(f"배치: JAB 다중 심볼 2×2 (마스터 1 + 슬레이브 3, 코드 {2 * side}×{2 * side} 모듈)")
            lines.append(f"프레임당 데이터: <b>{c['block_size']} B</b> (용량 {c['capacity']} B, 헤더 {HEADER_SIZE} B)")
            comp = "압축됨" if c["container"][4] & 1 else "무압축"
            lines.append(f"전송 크기: {human(len(c['container']))} ({comp}) → 소스 블록 K = <b>{k}</b>")
            lines.append(f"최소 필요 프레임 ≈ {k + 2} (어떤 프레임이든 무방)")
            lines.append(f"예상 시간 ≥ {fmt_time((k + 2) / fps)} (인식률 100% 가정)")
            if c["block_size"] * fps:
                lines.append(f"이론 속도: {human(c['block_size'] * fps)}/s")
        elif self.file_data is not None:
            lines.append("설정 확정 전 — 송신 시작 시 용량을 측정합니다.")
        else:
            lines.append("파일을 선택하세요. (창에 끌어다 놓기 가능)")
        if self.producer:
            real = 0.0
            if len(self.fps_hist) >= 2:
                dt = self.fps_hist[-1] - self.fps_hist[0]
                real = (len(self.fps_hist) - 1) / dt if dt > 0 else 0
            lines.append("<hr>")
            lines.append(f"표시한 프레임: <b>{self.shown}</b>")
            lines.append(f"실제 표시 속도: {real:.1f} fps (버퍼 {self.producer.buffered()})")
            if self.underruns:
                lines.append(f"<span style='color:#b45309'>생성 지연 {self.underruns}회 — fps를 낮추거나 버전을 줄이세요</span>")
        self.lbl_info.setText("<br>".join(lines))

    # ------------------------------------------------------------- 전체화면
    def toggle_full(self):
        if self.isFullScreen():
            self.side.show()
            self.showNormal()
        else:
            self.side.hide()
            self.showFullScreen()

    def closeEvent(self, e):
        self.stop()
        super().closeEvent(e)


def main():
    mp.freeze_support()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    w = MainWindow(sys.argv[1] if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]) else None)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
