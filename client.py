import sys, asyncio, json, logging, inspect, ssl, os, re, mimetypes

# Python 3.11+ compat fix
if not hasattr(inspect, "getargspec"):
    inspect.getargspec = getattr(inspect, "getfullargspec", None)

import vlc
from aiohttp import web as aiohttp_web
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QSlider, QLineEdit,
    QInputDialog, QStyle, QSizePolicy, QFrame, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QAction
from qasync import QEventLoop
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

NGROK_HEADERS = {"ngrok-skip-browser-warning": "true", "User-Agent": "VLCSyncDesktop/1.0"}
SSL_NO_VERIFY = ssl.create_default_context()
SSL_NO_VERIFY.check_hostname = False
SSL_NO_VERIFY.verify_mode = ssl.CERT_NONE
STREAM_PORT = 8766

DARK_BG = "#0f0f0f"
PANEL_BG = "#181818"
CTRL_BG = "#1c1c1c"
BTN_BG = "#2a2a2a"
BTN_HOV = "#3a3a3a"
ACCENT = "#ff6600"
TEXT = "#e8e8e8"
DIM = "#888888"
LOCKED = "#ff8800"
CTRL_C = "#44ff44"
STREAM_C = "#44aaff"

SL = "QSlider::groove:horizontal{{height:{h}px;background:#3a3a3a;border-radius:{r}px;}}QSlider::sub-page:horizontal{{background:{a};border-radius:{r}px;}}QSlider::handle:horizontal{{background:#fff;width:{hw}px;height:{hw}px;margin:-{m}px 0;border-radius:{hr}px;}}QSlider::handle:horizontal:disabled{{background:#555;}}"
BS = f"QPushButton{{background:{BTN_BG};border:none;border-radius:5px;color:{TEXT};font-size:13px;padding:5px 10px;}}QPushButton:hover{{background:{BTN_HOV};}}QPushButton:pressed{{background:#444;}}QPushButton:disabled{{color:#444;background:#1e1e1e;}}"
IB = f"QPushButton{{background:transparent;border:none;color:{TEXT};font-size:18px;padding:4px 8px;}}QPushButton:hover{{color:{ACCENT};}}QPushButton:pressed{{color:#cc5500;}}QPushButton:disabled{{color:#444;}}"


class OSDLabel(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background:rgba(0,0,0,180);color:#e8e8e8;font-size:15px;font-weight:bold;border-radius:8px;padding:8px 18px;")
        self.hide()
        self._t = QTimer(self, singleShot=True)
        self._t.timeout.connect(self.hide)

    def show_msg(self, text, ms=2000):
        self.setText(text); self.adjustSize()
        p = self.parent()
        self.move((p.width()-self.width())//2, p.height()-self.height()-70)
        self.show(); self.raise_(); self._t.start(ms)


class VideoWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{DARK_BG};")
        self._p = parent
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mouseDoubleClickEvent(self, e):
        if self._p: self._p.toggle_fullscreen()

    def wheelEvent(self, e):
        if self._p: self._p.wheelEvent(e)

    def keyPressEvent(self, e):
        if self._p: self._p.keyPressEvent(e)


class SeekSlider(QSlider):
    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setMaximum(10000)
        self.setStyleSheet(SL.format(h=5, r=3, a=ACCENT, hw=14, m=5, hr=7))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            v = int(e.position().x() / self.width() * self.maximum())
            self.setValue(v); self.sliderMoved.emit(v)
        super().mousePressEvent(e)


class LocalFileServer:
    def __init__(self):
        self.runner = None
        self.filepath = None

    async def start(self, filepath):
        self.filepath = filepath
        app = aiohttp_web.Application()
        app.router.add_get("/stream", self._handle)
        app.router.add_get("/", self._handle)
        self.runner = aiohttp_web.AppRunner(app)
        await self.runner.setup()
        await aiohttp_web.TCPSite(self.runner, "0.0.0.0", STREAM_PORT).start()
        logging.info(f"File server on port {STREAM_PORT}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            self.runner = self.filepath = None

    async def _handle(self, request):
        path = self.filepath
        if not path or not os.path.isfile(path):
            return aiohttp_web.Response(status=404, text="No file.")
        size = os.path.getsize(path)
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "application/octet-stream"
        rng = request.headers.get("Range")
        if rng:
            try:
                parts = rng.replace("bytes=", "").split("-")
                s = int(parts[0]) if parts[0] else 0
                e = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
                e = min(e, size - 1); ln = e - s + 1
                resp = aiohttp_web.StreamResponse(status=206, headers={
                    "Content-Type": mime, "Content-Length": str(ln),
                    "Content-Range": f"bytes {s}-{e}/{size}", "Accept-Ranges": "bytes"})
                await resp.prepare(request)
                with open(path, "rb") as f:
                    f.seek(s); rem = ln
                    while rem > 0:
                        chunk = f.read(min(262144, rem))
                        if not chunk: break
                        await resp.write(chunk); rem -= len(chunk)
                return resp
            except Exception as ex:
                return aiohttp_web.Response(status=500, text=str(ex))
        resp = aiohttp_web.StreamResponse(status=200, headers={
            "Content-Type": mime, "Content-Length": str(size), "Accept-Ranges": "bytes"})
        await resp.prepare(request)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk: break
                await resp.write(chunk)
        return resp


class CloudflareTunnel:
    def __init__(self):
        self._proc = None
        self.url = None

    async def start(self):
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "cloudflared", "tunnel", "--url", f"http://localhost:{STREAM_PORT}", "--no-autoupdate",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            logging.error("cloudflared not installed.")
            return None
        pat = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")
        try:
            while True:
                raw = await asyncio.wait_for(self._proc.stderr.readline(), timeout=30)
                line = raw.decode("utf-8", errors="ignore")
                m = pat.search(line)
                if m:
                    self.url = m.group(0)
                    logging.info(f"Tunnel: {self.url}")
                    return self.url
        except asyncio.TimeoutError:
            self.stop(); return None

    def stop(self):
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
            self._proc = None; self.url = None


class VLCSyncClient(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VLC Sync Desktop")
        self.resize(1100, 720)
        self.setStyleSheet(
            f"QMainWindow{{background:{DARK_BG};}}"
            f"QMenuBar{{background:{PANEL_BG};color:{TEXT};font-size:13px;}}"
            f"QMenuBar::item:selected{{background:#2e2e2e;}}"
            f"QMenu{{background:#222;color:{TEXT};border:1px solid #333;}}"
            f"QMenu::item:selected{{background:#3a3a3a;}}"
            f"QStatusBar{{background:{PANEL_BG};color:{DIM};font-size:11px;}}")

        self.is_controller = False
        self.ws = None
        self.server_url = "ws://localhost:8765"
        self.conn_task = None
        self.is_fullscreen = False
        self._sub_idx = 0; self._aud_idx = 1
        self.is_sharing = False
        self._file_server = LocalFileServer()
        self._cf_tunnel = CloudflareTunnel()

        self.instance = vlc.Instance("--no-xlib", "--network-caching=8000", "--file-caching=2000")
        self.media_player = self.instance.media_player_new()

        cw = QWidget(); cw.setStyleSheet(f"background:{DARK_BG};")
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self._build_menu()

        self.video_frame = VideoWidget(self)
        self.video_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.video_frame, stretch=1)
        self._osd_lbl = OSDLabel(self.video_frame)
        self._bind_vlc()

        self.ctrl_panel = QWidget(); self.ctrl_panel.setStyleSheet(f"background:{CTRL_BG};")
        cp = QVBoxLayout(self.ctrl_panel); cp.setContentsMargins(12, 8, 12, 10); cp.setSpacing(6)
        root.addWidget(self.ctrl_panel)
        cp.addLayout(self._build_seek_row())
        cp.addLayout(self._build_btn_row())

        self.lbl_status = QLabel("Disconnected")
        self.lbl_share = QLabel("")
        self.lbl_sync = QLabel("Sync: N/A")
        self.statusBar().addWidget(self.lbl_status)
        self.statusBar().addWidget(self.lbl_share)
        self.statusBar().addPermanentWidget(self.lbl_sync)

        QTimer(self, timeout=self._update_ui, interval=100).start()
        QTimer(self, timeout=self._broadcast, interval=300).start()
        self._lock_controls()
        self.trigger_reconnect()

    def _build_seek_row(self):
        row = QHBoxLayout(); row.setSpacing(8)
        self.lbl_curr = QLabel("00:00"); self.lbl_curr.setStyleSheet(f"color:{TEXT};font-size:12px;min-width:55px;")
        self.seek_bar = SeekSlider(); self.seek_bar.sliderMoved.connect(self.set_position)
        self.lbl_total = QLabel("00:00"); self.lbl_total.setStyleSheet(f"color:{DIM};font-size:12px;min-width:55px;")
        row.addWidget(self.lbl_curr); row.addWidget(self.seek_bar); row.addWidget(self.lbl_total)
        return row

    def _build_btn_row(self):
        row = QHBoxLayout(); row.setSpacing(4)

        def ibtn(px, tip, fn):
            b = QPushButton(); b.setIcon(self.style().standardIcon(px))
            b.setToolTip(tip); b.setStyleSheet(IB); b.clicked.connect(fn); return b

        def tbtn(txt, tip, fn, col=None):
            b = QPushButton(txt); b.setToolTip(tip)
            b.setStyleSheet(BS + (f"QPushButton{{color:{col};}}" if col else ""))
            b.clicked.connect(fn); return b

        self.btn_play = ibtn(QStyle.StandardPixmap.SP_MediaPlay, "Play/Pause (Space)", self.toggle_play_pause)
        self.btn_stop = ibtn(QStyle.StandardPixmap.SP_MediaStop, "Stop (S)", self.stop_video)
        row.addWidget(self.btn_play); row.addWidget(self.btn_stop); row.addWidget(self._sep())

        self.btn_b10 = tbtn("10s", "Back 10s (Left)", lambda: self.seek_rel(-10000))
        self.btn_f10 = tbtn("10s", "Fwd 10s (Right)", lambda: self.seek_rel(10000))
        row.addWidget(self.btn_b10); row.addWidget(self.btn_f10); row.addWidget(self._sep())

        self.btn_mute = QPushButton("Vol"); self.btn_mute.setFixedWidth(34)
        self.btn_mute.setStyleSheet(IB); self.btn_mute.clicked.connect(self.toggle_mute)
        self.vol_sl = QSlider(Qt.Orientation.Horizontal)
        self.vol_sl.setRange(0, 150); self.vol_sl.setValue(100); self.vol_sl.setFixedWidth(110)
        self.vol_sl.setStyleSheet(SL.format(h=4, r=2, a=ACCENT, hw=12, m=4, hr=6))
        self.vol_sl.valueChanged.connect(lambda v: (self.media_player.audio_set_volume(v), self.lbl_vol.setText(f"{v}%")))
        self.lbl_vol = QLabel("100%"); self.lbl_vol.setStyleSheet(f"color:{DIM};font-size:11px;min-width:38px;")
        row.addWidget(self.btn_mute); row.addWidget(self.vol_sl); row.addWidget(self.lbl_vol)
        row.addStretch(1)

        self.btn_sub = tbtn("CC", "Subtitles (V)", self.cycle_subtitle)
        self.btn_aud = tbtn("Audio", "Audio Track (B)", self.cycle_audio)
        self.btn_fs = ibtn(QStyle.StandardPixmap.SP_TitleBarMaxButton, "Fullscreen (F)", self.toggle_fullscreen)
        row.addWidget(self.btn_sub); row.addWidget(self.btn_aud); row.addWidget(self.btn_fs)
        row.addWidget(self._sep())

        self.btn_share = tbtn("Share Movie", "Stream movie to viewers", self.toggle_share, STREAM_C)
        row.addWidget(self.btn_share); row.addWidget(self._sep())

        self.lbl_role = QLabel("Viewer (Locked)")
        self.lbl_role.setStyleSheet(f"color:{LOCKED};font-size:12px;font-weight:bold;")
        self.btn_req = tbtn("Request Control", "Take playback control", self.request_control)
        row.addWidget(self.lbl_role); row.addWidget(self.btn_req)
        return row

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("Media")
        self._act(m, "Open Local File...", "Ctrl+O", self.open_file)
        self._act(m, "Share Movie (Stream)...", "Ctrl+Shift+S", self.toggle_share)
        self._act(m, "Stop Sharing", "", self.stop_sharing)
        m.addSeparator()
        self._act(m, "Quit", "Ctrl+Q", self.close)
        p = mb.addMenu("Playback")
        self._act(p, "Play / Pause", "Space", self.toggle_play_pause)
        self._act(p, "Stop", "S", self.stop_video)
        p.addSeparator()
        self._act(p, "+5 sec", "Shift+Right", lambda: self.seek_rel(5000))
        self._act(p, "-5 sec", "Shift+Left", lambda: self.seek_rel(-5000))
        self._act(p, "+10 sec", "Right", lambda: self.seek_rel(10000))
        self._act(p, "-10 sec", "Left", lambda: self.seek_rel(-10000))
        self._act(p, "+1 min", "Ctrl+Right", lambda: self.seek_rel(60000))
        self._act(p, "-1 min", "Ctrl+Left", lambda: self.seek_rel(-60000))
        a = mb.addMenu("Audio")
        self._act(a, "Cycle Language", "B", self.cycle_audio)
        self._act(a, "Mute", "M", self.toggle_mute)
        self._act(a, "Volume Up", "Up", self.vol_up)
        self._act(a, "Volume Down", "Down", self.vol_down)
        s = mb.addMenu("Subtitles")
        self._act(s, "Cycle Subtitles", "V", self.cycle_subtitle)
        y = mb.addMenu("Sync")
        self._act(y, "Connect to Server...", "", self.prompt_server_url)
        self._act(y, "Request Control", "", self.request_control)

    def _act(self, menu, label, shortcut, fn):
        a = QAction(label, self)
        if shortcut: a.setShortcut(shortcut)
        a.triggered.connect(fn); menu.addAction(a)

    def _sep(self):
        f = QFrame(); f.setFrameShape(QFrame.Shape.VLine); f.setStyleSheet("color:#333;"); return f

    def _bind_vlc(self):
        if sys.platform == "win32":
            self.media_player.set_hwnd(int(self.video_frame.winId()))
        elif sys.platform.startswith("linux"):
            self.media_player.set_xwindow(int(self.video_frame.winId()))
        elif sys.platform == "darwin":
            try: self.media_player.set_nsobject(int(self.video_frame.winId()))
            except Exception: pass

    @staticmethod
    def _fmt(ms):
        if ms is None or ms < 0: return "00:00"
        s = int(ms) // 1000; h, rem = divmod(s, 3600); m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _lock_controls(self):
        for w in [self.btn_play, self.btn_stop, self.seek_bar, self.btn_b10, self.btn_f10]:
            w.setEnabled(self.is_controller)
        self.btn_share.setEnabled(self.is_controller)
        if self.is_controller:
            self.lbl_role.setText("Controller")
            self.lbl_role.setStyleSheet(f"color:{CTRL_C};font-size:12px;font-weight:bold;")
        else:
            self.lbl_role.setText("Viewer (Locked)")
            self.lbl_role.setStyleSheet(f"color:{LOCKED};font-size:12px;font-weight:bold;")

    def osd(self, text, ms=2000):
        try: self._osd_lbl.show_msg(text, ms)
        except Exception: pass

    def keyPressEvent(self, event):
        k = event.key(); mod = event.modifiers()
        ctrl = bool(mod & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mod & Qt.KeyboardModifier.ShiftModifier)
        if k == Qt.Key.Key_Escape and self.is_fullscreen: self.toggle_fullscreen()
        elif k == Qt.Key.Key_F: self.toggle_fullscreen()
        elif k == Qt.Key.Key_Space: self.toggle_play_pause()
        elif k == Qt.Key.Key_S: self.stop_video()
        elif k == Qt.Key.Key_M: self.toggle_mute()
        elif k == Qt.Key.Key_V: self.cycle_subtitle()
        elif k == Qt.Key.Key_B: self.cycle_audio()
        elif k == Qt.Key.Key_Up: self.vol_up()
        elif k == Qt.Key.Key_Down: self.vol_down()
        elif k == Qt.Key.Key_Right:
            if ctrl: self.seek_rel(60000, "Fwd 1 min")
            elif shift: self.seek_rel(5000, "Fwd 5 sec")
            else: self.seek_rel(10000, "Fwd 10 sec")
        elif k == Qt.Key.Key_Left:
            if ctrl: self.seek_rel(-60000, "Back 1 min")
            elif shift: self.seek_rel(-5000, "Back 5 sec")
            else: self.seek_rel(-10000, "Back 10 sec")

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0: self.vol_up()
        else: self.vol_down()

    def open_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Open Video File", "",
            "Video Files (*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts *.m4v);;All Files (*)")
        if fname: self._load_path(fname)

    def _load_path(self, path_or_url):
        name = path_or_url.replace("\\", "/").split("/")[-1]
        self.setWindowTitle(f"{name} -- VLC Sync")
        m = self.instance.media_new(path_or_url)
        self.media_player.set_media(m)
        if self.is_controller: self.media_player.play()

    def toggle_play_pause(self):
        if not self.is_controller: return
        if self.media_player.is_playing():
            self.media_player.pause(); self.osd("Paused")
        else:
            self.media_player.play(); self.osd("Playing")

    def stop_video(self):
        if not self.is_controller: return
        self.media_player.stop(); self.osd("Stopped")

    def seek_rel(self, delta_ms, label=None):
        if not self.is_controller: return
        t = self.media_player.get_time()
        if t < 0: return
        self.media_player.set_time(max(0, t + delta_ms))
        if label is None:
            label = f"{'Fwd' if delta_ms > 0 else 'Back'} {abs(delta_ms)//1000}s"
        self.osd(label)

    def set_position(self, pos):
        if not self.is_controller: return
        self.media_player.set_position(pos / 10000.0)

    def toggle_mute(self):
        m = self.media_player.audio_get_mute()
        self.media_player.audio_set_mute(not m)
        self.osd("Muted" if not m else "Unmuted")

    def vol_up(self):
        v = min(150, self.vol_sl.value() + 5); self.vol_sl.setValue(v); self.osd(f"Vol {v}%")

    def vol_down(self):
        v = max(0, self.vol_sl.value() - 5); self.vol_sl.setValue(v); self.osd(f"Vol {v}%")

    def cycle_subtitle(self):
        tracks = self.media_player.video_get_spu_description()
        if not tracks or len(tracks) <= 1: self.osd("No subtitles"); return
        self._sub_idx = (self._sub_idx + 1) % len(tracks)
        tid, name = tracks[self._sub_idx]
        self.media_player.video_set_spu(tid)
        self.osd(f"Sub: {name.decode() if isinstance(name, bytes) else name}")

    def cycle_audio(self):
        tracks = self.media_player.audio_get_track_description()
        if not tracks or len(tracks) <= 1: self.osd("No audio tracks"); return
        self._aud_idx = (self._aud_idx + 1) % len(tracks)
        tid, name = tracks[self._aud_idx]
        self.media_player.audio_set_track(tid)
        self.osd(f"Audio: {name.decode() if isinstance(name, bytes) else name}")

    def toggle_share(self):
        if not self.is_controller: return
        if self.is_sharing: self.stop_sharing()
        else: asyncio.ensure_future(self._start_sharing_async())

    async def _start_sharing_async(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select Movie to Share", "",
            "Video Files (*.mp4 *.mkv *.avi *.mov *.flv *.wmv *.webm *.ts *.m4v);;All Files (*)")
        if not fname: return
        self.osd("Starting Cloudflare tunnel... (~10 sec)", 10000)
        self.lbl_share.setText("  Starting tunnel...")
        await self._file_server.start(fname)
        cf_url = await self._cf_tunnel.start()
        if not cf_url:
            self.osd("cloudflared not found! Run: winget install Cloudflare.cloudflared", 6000)
            self.lbl_share.setText("  Install cloudflared first")
            await self._file_server.stop()
            return
        stream_url = cf_url + "/stream"
        filename = os.path.basename(fname)
        self._load_path(fname)
        await self._safe_send({"type": "share_start", "stream_url": stream_url, "filename": filename})
        self.is_sharing = True
        self.btn_share.setText("Stop Sharing")
        self.btn_share.setStyleSheet(BS + "QPushButton{color:#ff4444;}")
        self.lbl_share.setText(f"  Sharing: {filename}")
        self.lbl_share.setStyleSheet(f"color:{STREAM_C};font-size:11px;")
        self.osd(f"Now streaming: {filename}", 4000)

    def stop_sharing(self):
        if not self.is_sharing: return
        self.is_sharing = False
        self._cf_tunnel.stop()
        asyncio.ensure_future(self._file_server.stop())
        asyncio.ensure_future(self._safe_send({"type": "share_stop"}))
        self.btn_share.setText("Share Movie")
        self.btn_share.setStyleSheet(BS + f"QPushButton{{color:{STREAM_C};}}")
        self.lbl_share.setText("")
        self.osd("Sharing stopped")

    def toggle_fullscreen(self):
        self.is_fullscreen = not self.is_fullscreen
        if self.is_fullscreen:
            self.menuBar().hide(); self.ctrl_panel.hide(); self.statusBar().hide()
            self.showFullScreen()
        else:
            self.menuBar().show(); self.ctrl_panel.show(); self.statusBar().show()
            self.showNormal()

    def prompt_server_url(self):
        url, ok = QInputDialog.getText(self, "Connect to Server",
            "Server URL (ws:// or wss://):", QLineEdit.EchoMode.Normal, self.server_url)
        if ok and url.strip():
            self.server_url = url.strip(); self.trigger_reconnect()

    def _update_ui(self):
        length = self.media_player.get_length()
        pos = self.media_player.get_position()
        self.lbl_total.setText(self._fmt(length))
        if length > 0:
            self.lbl_curr.setText(self._fmt(int(length * pos)))
            if not self.seek_bar.isSliderDown():
                self.seek_bar.blockSignals(True)
                self.seek_bar.setValue(int(pos * 10000))
                self.seek_bar.blockSignals(False)
        vol = self.media_player.audio_get_volume()
        if vol >= 0:
            self.vol_sl.blockSignals(True); self.vol_sl.setValue(vol); self.vol_sl.blockSignals(False)
            self.lbl_vol.setText(f"{vol}%")
        state = self.media_player.get_state()
        self.btn_play.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if state == vlc.State.Playing
            else QStyle.StandardPixmap.SP_MediaPlay))

    def trigger_reconnect(self):
        if self.conn_task and not self.conn_task.done():
            self.conn_task.cancel()
        self.conn_task = asyncio.ensure_future(self._connect_loop())

    async def _connect_loop(self):
        delay = 1
        while True:
            try:
                self.lbl_status.setText("Connecting...")
                use_ssl = SSL_NO_VERIFY if self.server_url.startswith("wss://") else None
                async with websockets.connect(
                    self.server_url, extra_headers=NGROK_HEADERS,
                    ssl=use_ssl, ping_interval=20, ping_timeout=60, open_timeout=10
                ) as ws:
                    self.ws = ws; delay = 1
                    self.lbl_status.setText(f"Connected: {self.server_url}")
                    async for raw in ws:
                        try: self._on_message(json.loads(raw))
                        except Exception as e: logging.warning(f"Bad msg: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.warning(f"Connection failed: {e}")
            finally:
                self.ws = None; self.is_controller = False
                self._lock_controls()
                self.lbl_status.setText(f"Disconnected - retrying in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    def _on_message(self, data):
        t = data.get("type")
        if t == "control_granted":
            self.is_controller = True; self._lock_controls(); self.osd("You have control")
        elif t in ("control_revoked", "control_denied"):
            self.is_controller = False; self.is_sharing = False; self._lock_controls()
            self.btn_share.setText("Share Movie")
            self.btn_share.setStyleSheet(BS + f"QPushButton{{color:{STREAM_C};}}")
            self.lbl_share.setText(""); self.osd("View-only mode")
        elif t == "sync" and not self.is_controller:
            self._apply_sync(data)
        elif t == "stream_available":
            fname = data.get("filename", "unknown")
            url = data.get("stream_url", "")
            if url:
                QTimer.singleShot(100, lambda: self._show_stream_dialog(fname, url))
        elif t == "stream_unavailable":
            self.lbl_share.setText(""); self.osd("Stream ended", 3000)
        elif t == "error":
            self.osd(f"Error: {data.get('msg', 'Unknown')}", 3000)

    def _show_stream_dialog(self, fname, stream_url):
        reply = QMessageBox.question(self, "Stream Available",
            f"The controller is sharing:\n\n  {fname}\n\nWould you like to play the stream?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self._load_path(stream_url)
            self.lbl_share.setText(f"  Streaming: {fname}")
            self.lbl_share.setStyleSheet(f"color:{STREAM_C};font-size:11px;")

    def _apply_sync(self, data):
        tstate = data.get("state"); tms = data.get("time")
        cstate = self.media_player.get_state(); cms = self.media_player.get_time()
        if cstate == vlc.State.NothingSpecial: return
        if tstate == vlc.State.Stopped.value:
            if cstate != vlc.State.Stopped: self.media_player.stop()
            return
        playing = (cstate == vlc.State.Playing); should = (tstate == vlc.State.Playing.value)
        if should and not playing: self.media_player.play()
        elif not should and playing: self.media_player.pause()
        if tms is not None and cms is not None and cms >= 0:
            drift = abs(cms - tms)
            if drift > 800:
                self.media_player.set_time(int(tms)); self.lbl_sync.setText(f"Corrected {drift}ms")
            else:
                self.lbl_sync.setText(f"Drift {drift}ms")

    def _broadcast(self):
        if not self.is_controller or not self.ws: return
        state = self.media_player.get_state()
        asyncio.ensure_future(self._safe_send({
            "type": "sync", "state": state.value if state else 0,
            "time": self.media_player.get_time()}))
        self.lbl_sync.setText("Broadcasting...")

    async def _safe_send(self, data: dict):
        if not self.ws: return
        try: await self.ws.send(json.dumps(data))
        except Exception as e: logging.debug(f"WS send skipped: {e}")

    def request_control(self):
        asyncio.ensure_future(self._safe_send({"type": "request_control"}))

    def closeEvent(self, event):
        self.stop_sharing()
        if self.conn_task: self.conn_task.cancel()
        self.media_player.stop(); self.instance.release(); event.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    win = VLCSyncClient()
    win.show()
    with loop:
        loop.run_forever()
