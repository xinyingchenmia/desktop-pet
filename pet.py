#!/usr/bin/env python3
"""
Desktop Pet — settings launcher + transparent overlay.
Run: python pet.py
Quit: Ctrl-C in the terminal.
"""

import os, sys, math, random, threading, time, io
from pathlib import Path
from dataclasses import dataclass

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QPushButton, QFileDialog, QGridLayout,
)
from PyQt6.QtCore import Qt, QTimer, QRect, QThread, pyqtSignal
from PyQt6.QtGui  import QPainter, QPixmap, QTransform, QDragEnterEvent, QDropEvent, QBrush, QColor, QPen, QPainterPath

try:
    import Quartz, AppKit
    MACOS = True
except ImportError:
    MACOS = False

try:
    from pynput import mouse as _mouse
    PYNPUT = True
except ImportError:
    PYNPUT = False

OWN_PID = os.getpid()
HERE    = Path(__file__).parent


# ── config ─────────────────────────────────────────────────────────────────────

@dataclass
class Config:
    wander_speed: float = 72.0
    chase_speed:  float = 190.0
    wander_min:   float = 2.5
    wander_max:   float = 6.0
    pet_height:   int   = 90   # all sprites scaled to same height → consistent cat size


# ── window detection ───────────────────────────────────────────────────────────

def active_window_rect() -> QRect | None:
    if not MACOS:
        return QApplication.primaryScreen().geometry()
    try:
        pid = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()
        if pid == OWN_PID:
            return None
        wins = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID)
        best_area, best_b = 0, None
        for w in wins:
            if w.get('kCGWindowOwnerPID') != pid: continue
            if w.get('kCGWindowLayer', 0)  != 0:  continue
            b    = w.get('kCGWindowBounds', {})
            area = b.get('Width', 0) * b.get('Height', 0)
            if area > best_area:
                best_area, best_b = area, b
        if best_b:
            return QRect(int(best_b['X']), int(best_b['Y']),
                         int(best_b['Width']), int(best_b['Height']))
    except Exception:
        pass
    return None


# ── image processing ───────────────────────────────────────────────────────────

def _defringe(img):
    """Replace semi-transparent edge pixels with colour of nearest opaque neighbour."""
    px = img.load()
    w, h = img.size
    out = img.copy()
    op  = out.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if 0 < a < 230:
                nr = ng = nb = count = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = x+dx, y+dy
                        if 0 <= nx < w and 0 <= ny < h:
                            er, eg, eb, ea = px[nx, ny]
                            if ea > 200:
                                nr += er; ng += eg; nb += eb; count += 1
                if count:
                    op[x, y] = (nr//count, ng//count, nb//count, a)
    return out


def process_image(src: str) -> 'Image':
    """Remove background (u2net), tight-crop, defringe."""
    from rembg import remove
    from PIL import Image

    img    = Image.open(src).convert('RGBA')
    result = remove(img)

    bbox = result.getbbox()
    if bbox:
        result = result.crop(bbox)

    return _defringe(result)


def generate_walk_frames(rest) -> list:
    """Generate 3 walk frames: bob + slight squish to suggest leg movement."""
    from PIL import Image
    w, h = rest.size
    frames = []
    # (y_offset, x_scale, y_scale) — compress slightly on the down-step
    transforms = [
        (-5, 1.00, 1.00),   # up
        ( 0, 1.03, 0.97),   # mid — slightly wider/squished (foot push)
        (-3, 1.00, 1.00),   # slight up
    ]
    for dy, sx, sy in transforms:
        new_w = int(w * sx)
        new_h = int(h * sy)
        scaled = rest.resize((new_w, new_h), Image.LANCZOS)
        frame  = Image.new('RGBA', (w, h), (0, 0, 0, 0))
        # centre horizontally, align to bottom
        px = (w - new_w) // 2
        py = h - new_h + dy
        frame.paste(scaled, (px, py), scaled)
        frames.append(frame)
    return frames


def pil_to_qpixmap(img) -> QPixmap:
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    pm = QPixmap()
    pm.loadFromData(buf.getvalue())
    return pm


class ProcessThread(QThread):
    finished = pyqtSignal(object)
    error    = pyqtSignal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

    def run(self):
        try:
            self.finished.emit(process_image(self.path))
        except Exception as e:
            self.error.emit(str(e))


# ── sprites ────────────────────────────────────────────────────────────────────

_SPRITES: dict = {}

def load_sprites(cfg: Config, images: dict):
    smooth = Qt.TransformationMode.SmoothTransformation
    # Scale every sprite to the same HEIGHT so the cat looks the same size in all poses.
    # (Scaling by width would make a wide sitting cat shorter than a tall walking cat.)
    rest_h = max(30, int(cfg.pet_height * 0.75))   # rest is 75% the size of walking
    _SPRITES['rest'] = pil_to_qpixmap(images['rest']).scaledToHeight(rest_h, smooth)
    for i in range(3):
        img = images.get(f'walk{i}', images['rest'])
        _SPRITES[f'walk{i}'] = pil_to_qpixmap(img).scaledToHeight(cfg.pet_height, smooth)

def _flipped(pm: QPixmap) -> QPixmap:
    return pm.transformed(QTransform().scale(-1, 1))

def draw_cat(painter: QPainter, win_w: int, win_h: int,
             frame: int, moving: bool, facing_right: bool):
    pm = _SPRITES.get(f'walk{frame % 3}', _SPRITES['rest']) if moving else _SPRITES['rest']
    if facing_right:
        pm = _flipped(pm)
    painter.drawPixmap((win_w - pm.width()) // 2, win_h - pm.height(), pm)


# ── pet logic ──────────────────────────────────────────────────────────────────

class Pet:
    ARRIVE = 6

    def __init__(self, cfg: Config):
        self.cfg = cfg
        s = QApplication.primaryScreen().geometry()
        self.x = self.tx = float(s.center().x())
        self.y = self.ty = float(s.center().y())
        self.spd          = cfg.wander_speed
        self.chasing      = False
        self.moving       = False
        self.facing_right = True
        self.bounds       = s
        self.frame        = 0
        self.frame_t      = 0.0
        self.next_wander  = random.uniform(cfg.wander_min, cfg.wander_max)

    @property
    def margin(self):
        return self.cfg.pet_height // 2 + 4

    def set_bounds(self, r: QRect):
        self.bounds = r
        m = self.margin
        self.x  = max(r.x()+m, min(r.right()-m,  self.x))
        self.y  = max(r.y()+m, min(r.bottom()-m, self.y))
        self.tx = max(r.x()+m, min(r.right()-m,  self.tx))
        self.ty = max(r.y()+m, min(r.bottom()-m, self.ty))

    def on_click(self, gx: float, gy: float):
        m, b = self.margin, self.bounds
        self.tx      = max(b.x()+m, min(b.right()-m,  gx))
        self.ty      = max(b.y()+m, min(b.bottom()-m, gy))
        self.chasing = True
        self.spd     = self.cfg.chase_speed

    def update(self, dt: float):
        self.frame_t += dt
        if self.frame_t >= (0.09 if self.chasing else 0.14):
            self.frame_t  = 0.0
            self.frame    = (self.frame + 1) % 4

        if not self.chasing:
            self.next_wander -= dt
            if self.next_wander <= 0:
                self._pick_wander()

        dx, dy = self.tx - self.x, self.ty - self.y
        d      = math.hypot(dx, dy)
        prev_x, prev_y = self.x, self.y

        if d > self.ARRIVE:
            step          = min(self.spd * dt, d)
            self.x       += dx/d * step
            self.y       += dy/d * step
            self.facing_right = dx >= 0
            self.moving   = True
        else:
            self.x, self.y = self.tx, self.ty
            self.moving    = False
            if self.chasing:
                self.chasing     = False
                self.spd         = self.cfg.wander_speed
                self.next_wander = random.uniform(self.cfg.wander_min, self.cfg.wander_max)

        m, b  = self.margin, self.bounds
        self.x = max(b.x()+m, min(b.right()-m,  self.x))
        self.y = max(b.y()+m, min(b.bottom()-m, self.y))

        if self.moving and math.hypot(self.x-prev_x, self.y-prev_y) < 0.5:
            self.moving      = False
            self.chasing     = False
            self.next_wander = random.uniform(1.0, 2.5)

    def _pick_wander(self):
        m, b = self.margin + 10, self.bounds
        if b.width() > m*2 and b.height() > m*2:
            self.tx = random.uniform(b.x()+m, b.right()-m)
            self.ty = random.uniform(b.y()+m, b.bottom()-m)
        self.next_wander = random.uniform(self.cfg.wander_min, self.cfg.wander_max)
        self.spd         = self.cfg.wander_speed



class HomeButton(QWidget):
    """Small house icon pinned to the top-right of the active window."""
    go_home = pyqtSignal(float, float)   # global screen coords of house centre
    def __init__(self, size: int = 90):
        super().__init__()
        pm = QPixmap(str(HERE / 'house.PNG'))
        self._pm = pm.scaled(size, size,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        w, h = self._pm.width(), self._pm.height()
        self.setFixedSize(w, h)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Send pet home')

        # show off-screen first so the native window handle is valid for macOS config
        self.move(-9999, -9999)
        self.show()

        if MACOS:
            try:
                import objc
                from AppKit import (NSColor, NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowCollectionBehaviorFullScreenAuxiliary)
                ns = objc.objc_object(c_void_p=int(self.winId())).window()
                ns.setHidesOnDeactivate_(False)
                ns.setHasShadow_(False)
                ns.setOpaque_(False)
                ns.setBackgroundColor_(NSColor.clearColor())
                ns.setLevel_(NSStatusWindowLevel)
                ns.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehaviorStationary
                    | NSWindowCollectionBehaviorFullScreenAuxiliary)
            except Exception as e:
                print(f'[home] {e}')

    def update_pos(self, bounds: QRect):
        w, h = self._pm.width(), self._pm.height()
        self.move(bounds.right() - w - 2, bounds.top() + 2)

    def mousePressEvent(self, _):
        c = self.mapToGlobal(self.rect().center())
        self.go_home.emit(float(c.x()), float(c.y()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.drawPixmap(0, 0, self._pm)
        p.end()


# ── overlay ────────────────────────────────────────────────────────────────────

class Overlay(QWidget):
    def __init__(self, cfg: Config):
        super().__init__()
        sz      = cfg.pet_height + 60
        self.W  = sz
        self.H  = sz
        self.setFixedSize(sz, sz)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.show()

        if MACOS:
            try:
                import objc
                from AppKit import (NSColor, NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces,
                    NSWindowCollectionBehaviorStationary,
                    NSWindowCollectionBehaviorFullScreenAuxiliary)
                ns = objc.objc_object(c_void_p=int(self.winId())).window()
                ns.setIgnoresMouseEvents_(True)
                ns.setHidesOnDeactivate_(False)
                ns.setHasShadow_(False)
                ns.setOpaque_(False)
                ns.setBackgroundColor_(NSColor.clearColor())
                ns.setLevel_(NSStatusWindowLevel)
                ns.setCollectionBehavior_(
                    NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehaviorStationary
                    | NSWindowCollectionBehaviorFullScreenAuxiliary)
            except Exception as e:
                print(f'[pet] {e}')

        self.pet         = Pet(cfg)
        self._click      = None
        self._lock       = threading.Lock()
        self._t          = time.monotonic()
        self._going_home = False

        self._home = HomeButton(cfg.pet_height * 2)
        self._home.go_home.connect(self._on_go_home)

        QTimer(self, timeout=self._tick,        interval=16 ).start()
        QTimer(self, timeout=self._sync_bounds, interval=400).start()

        if PYNPUT:
            def _on_click(gx, gy, btn, pressed):
                if pressed and btn == _mouse.Button.left:
                    with self._lock:
                        self._click = (float(gx), float(gy))
            ml = _mouse.Listener(on_click=_on_click)
            ml.daemon = True
            ml.start()

        self._sync_bounds()
        # retry quickly at startup until another app's window is detected
        self._startup_retries = 20
        QTimer(self, timeout=self._startup_sync, interval=100).start()

    def _startup_sync(self):
        if self._startup_retries <= 0:
            return
        self._startup_retries -= 1
        r = active_window_rect()
        if r and r.width() > 80 and r.height() > 80:
            self.pet.set_bounds(r)
            self._home.update_pos(r)
            self._startup_retries = 0   # found it — stop retrying

    def _on_go_home(self, x: float, y: float):
        self._going_home = True
        self.pet.on_click(x, y)

    def _sync_bounds(self):
        r = active_window_rect()
        if r and r.width() > 80 and r.height() > 80:
            self.pet.set_bounds(r)
            self._home.update_pos(r)

    def _tick(self):
        now = time.monotonic()
        dt  = min(now - self._t, 0.05)
        self._t = now

        with self._lock:
            click, self._click = self._click, None
        # ignore random clicks once heading home
        if click and not self._going_home:
            self.pet.on_click(*click)

        self.pet.update(dt)
        self.move(int(self.pet.x) - self.W//2, int(self.pet.y) - self.H)
        self.update()

        # quit once pet has arrived home and settled
        if self._going_home and not self.pet.moving and not self.pet.chasing:
            QTimer.singleShot(500, QApplication.quit)
            self._going_home = False   # prevent double-fire

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        draw_cat(p, self.W, self.H, self.pet.frame, self.pet.moving, self.pet.facing_right)
        p.end()


# ── launcher UI ────────────────────────────────────────────────────────────────

STYLE = '''
QWidget        { background: #1e1e1e; color: #eee;
                 font-family: "Helvetica Neue", Arial, sans-serif; }
QLabel         { color: #ddd; }
QSlider::groove:horizontal  { height: 4px; background: #3a3a3a; border-radius: 2px; }
QSlider::sub-page:horizontal{ background: #4a9eff; border-radius: 2px; }
QSlider::handle:horizontal  { background: #4a9eff; width: 16px; height: 16px;
                               margin: -6px 0; border-radius: 8px; }
QPushButton    { background: #4a9eff; color: white; border: none;
                 border-radius: 8px; padding: 10px 24px; font-size: 14px; font-weight: 600; }
QPushButton:hover    { background: #2f80ed; }
QPushButton:disabled { background: #3a3a3a; color: #666; }
'''


class PhotoSlot(QWidget):
    """Click or drag-drop to upload a photo; processes in background."""
    changed = pyqtSignal()

    def __init__(self, label: str, required: bool = False):
        super().__init__()
        self.label     = label
        self.required  = required
        self.pil_image = None
        self._thread   = None
        self.setAcceptDrops(True)
        self.setFixedSize(108, 130)   # extra height for clear button

        self._lbl = QLabel(f'+ {label}', self)
        self._lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl.setGeometry(0, 0, 108, 108)
        self._lbl.setWordWrap(True)
        self._lbl.setCursor(Qt.CursorShape.PointingHandCursor)

        # × clear button — only shown when a photo is loaded (non-required slots)
        self._btn_clear = QPushButton('× clear', self)
        self._btn_clear.setGeometry(0, 112, 108, 18)
        self._btn_clear.setStyleSheet(
            'QPushButton { background: transparent; color: #666; font-size: 10px;'
            '  border: none; padding: 0; }'
            'QPushButton:hover { color: #e74c3c; }')
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear.hide()
        self._btn_clear.clicked.connect(self.clear)

        self._set_style('empty')

    def _set_style(self, state: str):
        base = 'border-radius: 8px; font-size: 11px;'
        if state == 'empty':
            c = '#e74c3c' if self.required else '#444'
            self._lbl.setStyleSheet(f'QLabel {{ border: 2px dashed {c}; color: #888; background: #252525; {base} }}')
        elif state == 'loading':
            self._lbl.setStyleSheet(f'QLabel {{ border: 2px solid #4a9eff; color: #4a9eff; background: #252525; {base} }}')
        elif state == 'done':
            self._lbl.setStyleSheet(f'QLabel {{ border: 2px solid #2ecc71; background: #1a1a1a; {base} }}')
        elif state == 'error':
            self._lbl.setStyleSheet(f'QLabel {{ border: 2px solid #e74c3c; color: #e74c3c; background: #252525; {base} }}')

    def mousePressEvent(self, e):
        if self._lbl.geometry().contains(e.pos()):
            self._pick()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls(): e.acceptProposedAction()
    def dropEvent(self, e: QDropEvent):
        urls = e.mimeData().urls()
        if urls: self._start(urls[0].toLocalFile())

    def _pick(self):
        path, _ = QFileDialog.getOpenFileName(
            self, f'Select image — {self.label}', '', 'Images (*.png *.jpg *.jpeg *.webp *.bmp)')
        if path: self._start(path)

    def _start(self, path: str):
        self._lbl.clear()
        self._lbl.setText('Processing…')
        self._set_style('loading')
        self._btn_clear.hide()
        self._thread = ProcessThread(path)
        self._thread.finished.connect(self._done)
        self._thread.error.connect(self._err)
        self._thread.start()

    def _done(self, img):
        self.pil_image = img
        pm = pil_to_qpixmap(img).scaled(
            100, 100,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._lbl.setPixmap(pm)
        self._set_style('done')
        if not self.required:
            self._btn_clear.show()
        self.changed.emit()

    def _err(self, msg):
        self._lbl.setText('❌ Failed')
        self._set_style('error')
        print(f'[slot] {self.label}: {msg}')

    def clear(self):
        """Reset slot to empty — walk frames will be auto-generated."""
        self.pil_image = None
        self._lbl.clear()
        self._lbl.setText(f'+ {self.label}')
        self._set_style('empty')
        self._btn_clear.hide()
        self.changed.emit()

    def load_pil(self, img):
        """Load an already-processed PIL image (used for existing files)."""
        self.pil_image = img
        pm = pil_to_qpixmap(img).scaled(
            100, 100,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._lbl.setPixmap(pm)
        self._set_style('done')
        if not self.required:
            self._btn_clear.show()


class Launcher(QWidget):
    def __init__(self):
        super().__init__()
        self.overlay = None
        self.setWindowTitle('Desktop Pet')
        self.setFixedWidth(500)
        self.setStyleSheet(STYLE)
        self._build()
        self._load_existing()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(18)
        root.setContentsMargins(28, 28, 28, 28)

        # ── header ──
        title = QLabel('🐱  Desktop Pet')
        title.setStyleSheet('font-size: 24px; font-weight: bold; color: #fff;')
        root.addWidget(title)

        # ── movement settings ──
        root.addWidget(self._section('Movement'))

        self.sl_wander = self._slider(10,  200,  72)
        self.sl_chase  = self._slider(50,  400,  190)
        self.sl_freq   = self._slider(1,   20,   4)
        self.sl_size   = self._slider(40,  160,  90)

        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setColumnStretch(1, 1)

        for i, (name, desc, sl, lb) in enumerate([
            ('Wander Speed',    'speed when roaming randomly',        self.sl_wander, self.sl_wander._lb),
            ('Chase Speed',     'speed when running to a click',      self.sl_chase,  self.sl_chase._lb),
            ('Wander Interval', 'seconds between random moves',       self.sl_freq,   self.sl_freq._lb),
            ('Size',            'height in px — same for all poses',  self.sl_size,   self.sl_size._lb),
        ]):
            col = QVBoxLayout()
            col.setSpacing(1)
            lname = QLabel(name)
            lname.setStyleSheet('color: #ccc; font-size: 12px; font-weight: 600;')
            ldesc = QLabel(desc)
            ldesc.setStyleSheet('color: #555; font-size: 10px;')
            col.addWidget(lname)
            col.addWidget(ldesc)
            w = QWidget(); w.setLayout(col)
            grid.addWidget(w,  i, 0)
            grid.addWidget(sl, i, 1)
            grid.addWidget(lb, i, 2)

        root.addLayout(grid)

        # ── photos ──
        root.addWidget(self._section('Pet Photos'))

        hint = QLabel('Upload photos — background is removed automatically. Walk frames are optional; if skipped, they\'re generated from the rest photo.')
        hint.setStyleSheet('color: #555; font-size: 11px;')
        hint.setWordWrap(True)
        root.addWidget(hint)

        photos = QHBoxLayout()
        photos.setSpacing(8)
        self.s_rest  = PhotoSlot('Rest\n(required)', required=True)
        self.s_walk1 = PhotoSlot('Walk 1')
        self.s_walk2 = PhotoSlot('Walk 2')
        self.s_walk3 = PhotoSlot('Walk 3')
        for s in (self.s_rest, self.s_walk1, self.s_walk2, self.s_walk3):
            photos.addWidget(s)
            s.changed.connect(self._refresh_btn)
        root.addLayout(photos)

        # ── launch ──
        self.btn = QPushButton('Launch Pet  →')
        self.btn.setEnabled(False)
        self.btn.setFixedHeight(46)
        self.btn.clicked.connect(self._launch)
        root.addWidget(self.btn)

    # ── helpers ────────────────────────────────────────────────

    def _section(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setStyleSheet(
            'font-size: 12px; font-weight: 600; color: #888; '
            'border-bottom: 1px solid #333; padding-bottom: 6px; margin-top: 4px;')
        return lb

    def _slider(self, mn: int, mx: int, val: int, unit: str = '') -> QSlider:
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(mn, mx)
        sl.setValue(val)
        sl._lb = QLabel(str(val))
        sl._lb.setFixedWidth(52)
        sl._lb.setStyleSheet('color: #888; font-size: 11px;')
        sl.valueChanged.connect(lambda v, lb=sl._lb: lb.setText(str(v)))
        return sl

    def _refresh_btn(self):
        self.btn.setEnabled(self.s_rest.pil_image is not None)

    def _load_existing(self):
        """Pre-populate slots from previously saved images."""
        from PIL import Image
        for path, slot in [
            (HERE/'cat_rest.png',  self.s_rest),
            (HERE/'cat_walk1.png', self.s_walk1),
            (HERE/'cat_walk2.png', self.s_walk2),
            (HERE/'cat_walk3.png', self.s_walk3),
        ]:
            if path.exists():
                try:
                    slot.load_pil(Image.open(str(path)).convert('RGBA'))
                except Exception:
                    pass
        self._refresh_btn()

    def _launch(self):
        cfg = Config(
            wander_speed = float(self.sl_wander.value()),
            chase_speed  = float(self.sl_chase.value()),
            wander_min   = max(1.0, self.sl_freq.value() * 0.5),
            wander_max   = float(self.sl_freq.value()),
            pet_height   = self.sl_size.value(),
        )

        rest   = self.s_rest.pil_image
        walks  = [self.s_walk1.pil_image,
                  self.s_walk2.pil_image,
                  self.s_walk3.pil_image]
        images = {'rest': rest}

        if all(w is None for w in walks):
            for i, img in enumerate(generate_walk_frames(rest)):
                images[f'walk{i}'] = img
        else:
            for i, img in enumerate(walks):
                images[f'walk{i}'] = img if img is not None else rest

        load_sprites(cfg, images)
        self.hide()

        if MACOS:
            try:
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
                NSApplication.sharedApplication().setActivationPolicy_(
                    NSApplicationActivationPolicyAccessory)
            except Exception:
                pass

        self.overlay = Overlay(cfg)

    def closeEvent(self, e):
        if self.overlay is None:
            QApplication.quit()
        e.accept()


# ── entry ──────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    launcher = Launcher()
    launcher.show()
    launcher.raise_()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
