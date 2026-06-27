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
from PyQt6.QtCore import Qt, QTimer, QRect, QPoint, QThread, pyqtSignal
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

# pynput's keyboard.Listener spins its own thread, but on macOS some HIToolbox
# calls it triggers (TSMGetInputSourceProperty) assert they run on the main
# thread and crash with SIGTRAP otherwise — so on macOS we use a CGEventTap
# on the main run loop instead. Off macOS, fall back to pynput.keyboard.
if not MACOS:
    try:
        from pynput import keyboard as _keyboard
        PYNPUT_KEYBOARD = True
    except ImportError:
        PYNPUT_KEYBOARD = False
else:
    PYNPUT_KEYBOARD = False

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
             frame: int, moving: bool, facing_right: bool) -> int:
    """Draws the cat and returns the y-coordinate of its top (head)."""
    pm = _SPRITES.get(f'walk{frame % 3}', _SPRITES['rest']) if moving else _SPRITES['rest']
    if facing_right:
        pm = _flipped(pm)
    top = win_h - pm.height()
    painter.drawPixmap((win_w - pm.width()) // 2, top, pm)
    return top


def draw_hunger_bar(painter: QPainter, win_w: int, hunger: float, cat_top: int) -> int:
    """Draws the hunger bar and returns the y-coordinate of its top."""
    bar_w, bar_h = 36, 5
    x = (win_w - bar_w) // 2
    y = max(2, cat_top - bar_h - 4)
    pct = max(0.0, min(1.0, hunger / 100.0))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(40, 40, 40, 150)))
    painter.drawRoundedRect(x, y, bar_w, bar_h, 2, 2)

    if pct < 0.3:
        fill = QColor(231, 76, 60)
    elif pct < 0.6:
        fill = QColor(241, 196, 15)
    else:
        fill = QColor(46, 204, 113)
    painter.setBrush(QBrush(fill))
    painter.drawRoundedRect(x, y, max(1, int(bar_w * pct)), bar_h, 2, 2)

    if hunger < 20:
        painter.setPen(QPen(QColor(255, 255, 255)))
        f = painter.font()
        f.setPointSize(9)
        painter.setFont(f)
        painter.drawText(x - 10, y - 2, bar_w + 20, 16,
                          Qt.AlignmentFlag.AlignCenter, '🍖')

    return y


def draw_speech_bubble(painter: QPainter, win_w: int, text: str, bottom_y: int):
    """Draws a speech bubble whose bottom-tip sits just above bottom_y."""
    if not text:
        return
    painter.save()
    f = painter.font()
    f.setPointSize(9)
    f.setBold(True)
    painter.setFont(f)
    metrics = painter.fontMetrics()

    pad_x, pad_y = 7, 4
    text_w   = min(metrics.horizontalAdvance(text), win_w - 10)
    bubble_w = text_w + pad_x * 2
    bubble_h = metrics.height() + pad_y * 2
    x = max(2, (win_w - bubble_w) // 2)
    y = max(2, bottom_y - bubble_h - 6)

    painter.setPen(QPen(QColor(90, 90, 90), 1))
    painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
    painter.drawRoundedRect(x, y, bubble_w, bubble_h, 7, 7)

    cx = win_w // 2
    tri = QPainterPath()
    tri.moveTo(cx - 5, y + bubble_h - 1)
    tri.lineTo(cx + 5, y + bubble_h - 1)
    tri.lineTo(cx, y + bubble_h + 5)
    tri.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255, 235)))
    painter.drawPath(tri)

    painter.setPen(QPen(QColor(40, 40, 40)))
    painter.drawText(QRect(x, y, bubble_w, bubble_h),
                      Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


# ── pet logic ──────────────────────────────────────────────────────────────────

class Pet:
    ARRIVE        = 6
    HUNGER_FULL   = 100.0
    HUNGER_DECAY  = 100.0 / 1800.0   # fully empties in 30 minutes if never fed
    KEYS_PER_BOWL = 5000             # keystrokes needed to earn one bowl of food

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
        self.hunger         = self.HUNGER_FULL
        self.bowls          = 0   # complete bowls of food, earned and ready to feed
        self.progress_keys  = 0   # keystrokes typed toward the next bowl
        self._key_pending   = 0   # buffer filled by the key-tap callback
        self.working        = True   # False = paused, keystrokes don't count
        self.speech          = ''
        self.speech_t        = 0.0

    def add_keystrokes(self, count: int):
        self._key_pending += count

    def toggle_work(self):
        self.working = not self.working
        self.speech   = '现在主人努力打猎中' if self.working else '现在主人在摸鱼中'
        self.speech_t = 2.5

    def feed(self):
        """Consume one bowl of food — fully satiates the pet."""
        if self.bowls > 0:
            self.bowls  -= 1
            self.hunger  = self.HUNGER_FULL

    @property
    def bowl_progress(self) -> float:
        return self.progress_keys / self.KEYS_PER_BOWL

    @property
    def hunger_speed_factor(self) -> float:
        """Starving pets move sluggishly."""
        if self.hunger >= 40:
            return 1.0
        return 0.4 + 0.6 * (self.hunger / 40.0)

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

        # keystrokes fill the current bowl; once full, a bowl becomes available
        # (only while "working" — paused while slacking off)
        if self._key_pending:
            if self.working:
                self.progress_keys += self._key_pending
                while self.progress_keys >= self.KEYS_PER_BOWL:
                    self.progress_keys -= self.KEYS_PER_BOWL
                    self.bowls         += 1
            self._key_pending = 0
        self.hunger = max(0.0, self.hunger - self.HUNGER_DECAY * dt)

        if self.speech_t > 0:
            self.speech_t -= dt
            if self.speech_t <= 0:
                self.speech = ''

        if not self.chasing:
            self.next_wander -= dt
            if self.next_wander <= 0:
                self._pick_wander()

        dx, dy = self.tx - self.x, self.ty - self.y
        d      = math.hypot(dx, dy)
        prev_x, prev_y = self.x, self.y

        if d > self.ARRIVE:
            speed         = self.spd * self.hunger_speed_factor
            step          = min(speed * dt, d)
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
        self._native_ready = False

    def _setup_native(self):
        if self._native_ready or not MACOS:
            return
        self._native_ready = True
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
        if not self.isVisible():
            self.show()
            self._setup_native()

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


def _circular_pixmap(src: QPixmap, d: int) -> QPixmap:
    """Fit src inside a solid-white circle of diameter d (a sticker/badge look)."""
    pad   = max(2, int(d * 0.12))   # margin so the food doesn't touch the edge
    inner = d - pad * 2
    scaled = src.scaled(inner, inner, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    result = QPixmap(d, d)
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, d, d)
    p.setClipPath(path)
    p.fillRect(0, 0, d, d, QColor(255, 255, 255))

    x = (d - scaled.width())  // 2
    y = (d - scaled.height()) // 2
    p.drawPixmap(x, y, scaled)
    p.end()
    return result


class FoodButton(QWidget):
    """Cat-food bowl pinned to the top-left of the active window.
    A ring around the bowl fills as you type — full ring = one bowl earned.
    The red badge shows how many whole bowls are stockpiled; click feeds one."""
    feed_clicked = pyqtSignal()

    LABEL_H = 30

    def __init__(self, size: int = 64):
        super().__init__()
        self._size      = size
        self._width     = max(size, 96)   # extra width to fit food% + hunger%
        self.progress   = 0.0   # 0..1 progress toward the next bowl
        self.bowls      = 0     # whole bowls stockpiled, ready to feed
        self.keys_now   = 0
        self.keys_goal  = Pet.KEYS_PER_BOWL
        self.hunger_pct = 100
        self.setFixedSize(self._width, size + self.LABEL_H)

        icon_d = max(8, size - 2 * (4 + 4))   # matches the inset used when drawing
        self._icon_pm = _circular_pixmap(QPixmap(str(HERE / 'food_clean.png')), icon_d)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip('Click to feed the cat')
        self._native_ready = False

    def _setup_native(self):
        if self._native_ready or not MACOS:
            return
        self._native_ready = True
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
            print(f'[food] {e}')

    def update_pos(self, bounds: QRect):
        self.move(bounds.x() + 2, bounds.top() + 2)
        if not self.isVisible():
            self.show()
            self._setup_native()

    def set_progress(self, progress: float, bowls: int, keys_now: int,
                      keys_goal: int, hunger_pct: int):
        progress = max(0.0, min(1.0, progress))
        if (progress != self.progress or bowls != self.bowls
                or keys_now != self.keys_now or hunger_pct != self.hunger_pct):
            self.progress   = progress
            self.bowls      = bowls
            self.keys_now   = keys_now
            self.keys_goal  = keys_goal
            self.hunger_pct = hunger_pct
            self.update()

    def mousePressEvent(self, _):
        self.feed_clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        s        = self._size
        icon_x   = (self._width - s) // 2
        icon_rect = QRect(icon_x, 0, s, s)
        ring     = 4
        ring_rect = icon_rect.adjusted(ring//2 + 1, ring//2 + 1,
                                        -(ring//2 + 1), -(ring//2 + 1))

        # background ring (unfilled track)
        p.setPen(QPen(QColor(0, 0, 0, 50), ring))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(ring_rect, 0, 360 * 16)

        # progress ring — fills clockwise from the top as you type
        if self.progress > 0:
            p.setPen(QPen(QColor(46, 204, 113), ring))
            span = -int(self.progress * 360 * 16)
            p.drawArc(ring_rect, 90 * 16, span)

        # food icon (image)
        px = icon_x + (s - self._icon_pm.width()) // 2
        py = (s - self._icon_pm.height()) // 2
        p.drawPixmap(px, py, self._icon_pm)

        # red badge — whole bowls stockpiled
        if self.bowls > 0:
            bd = max(15, s // 3)
            bx, by = icon_x + s - bd - 1, s - bd - 1
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(231, 76, 60)))
            p.drawEllipse(bx, by, bd, bd)
            bf = p.font()
            bf.setPointSize(max(8, int(bd * 0.5)))
            bf.setBold(True)
            p.setFont(bf)
            p.setPen(QPen(QColor(255, 255, 255)))
            p.drawText(bx, by, bd, bd, Qt.AlignmentFlag.AlignCenter,
                       str(min(self.bowls, 99)))

        # label below the icon: "keys earned / goal", then food% next to hunger%
        label_rect = QRect(0, s, self._width, self.LABEL_H)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(30, 30, 30, 175)))
        p.drawRoundedRect(label_rect.adjusted(0, 0, 0, -2), 6, 6)

        p.setPen(QPen(QColor(255, 255, 255)))
        f1 = p.font()
        f1.setPointSize(9)
        f1.setBold(True)
        p.setFont(f1)
        p.drawText(label_rect.adjusted(0, 2, 0, -15), Qt.AlignmentFlag.AlignCenter,
                   f'{self.keys_now}/{self.keys_goal}')

        food_pct = int(self.progress * 100)
        if self.hunger_pct < 30:
            hunger_color = QColor(231, 76, 60)
        elif self.hunger_pct < 60:
            hunger_color = QColor(241, 196, 15)
        else:
            hunger_color = QColor(120, 230, 160)

        f2 = p.font()
        f2.setPointSize(8)
        f2.setBold(False)
        p.setFont(f2)
        row2 = label_rect.adjusted(0, 15, 0, -2)
        half = row2.width() // 2

        food_rect = QRect(row2.x(), row2.y(), half, row2.height())
        p.setPen(QPen(QColor(200, 230, 210)))
        p.drawText(food_rect, Qt.AlignmentFlag.AlignCenter, f'🍖{food_pct}%')

        hunger_rect = QRect(row2.x() + half, row2.y(), row2.width() - half, row2.height())
        p.setPen(QPen(hunger_color))
        p.drawText(hunger_rect, Qt.AlignmentFlag.AlignCenter, f'❤{self.hunger_pct}%')
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
        self._keys       = 0
        self._lock       = threading.Lock()
        self._t          = time.monotonic()
        self._going_home = False

        self._home = HomeButton(cfg.pet_height * 2)
        self._home.go_home.connect(self._on_go_home)

        self._food_btn = FoodButton(max(48, int(cfg.pet_height * 0.7)))
        self._food_btn.feed_clicked.connect(self._on_feed_click)

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

        if MACOS:
            self._setup_key_tap()
        elif PYNPUT_KEYBOARD:
            def _on_key(_key):
                with self._lock:
                    self._keys += 1
            kl = _keyboard.Listener(on_press=_on_key)
            kl.daemon = True
            kl.start()

        self._sync_bounds()
        # retry quickly at startup until another app's window is detected
        self._startup_retries = 20
        QTimer(self, timeout=self._startup_sync, interval=100).start()

    def _on_feed_click(self):
        self.pet.feed()
        self._food_btn.set_progress(self.pet.bowl_progress, self.pet.bowls,
                                     self.pet.progress_keys, Pet.KEYS_PER_BOWL,
                                     int(self.pet.hunger))

    def _startup_sync(self):
        if self._startup_retries <= 0:
            return
        self._startup_retries -= 1
        r = active_window_rect()
        if r and r.width() > 80 and r.height() > 80:
            self.pet.set_bounds(r)
            self._home.update_pos(r)
            self._food_btn.update_pos(r)
            self._startup_retries = 0   # found it — stop retrying

    def _setup_key_tap(self):
        """Global key-press tap on the main run loop (Quartz, not pynput —
        avoids a macOS HIToolbox thread-assert crash when keyboard hooks
        run off the main thread)."""
        def _callback(_proxy, _type, event, _refcon):
            with self._lock:
                self._keys += 1
            return event
        try:
            tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap,
                Quartz.kCGHeadInsertEventTap,
                Quartz.kCGEventTapOptionListenOnly,
                Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
                _callback,
                None)
            if not tap:
                print('[pet] could not create key tap — enable Input Monitoring '
                      'permission in System Settings to feed via typing')
                return
            src = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
            Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                       Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(tap, True)
            self._key_tap = tap   # keep references alive
            self._key_tap_src = src
        except Exception as e:
            print(f'[pet] key tap setup failed: {e}')

    def _on_go_home(self, x: float, y: float):
        self._going_home = True
        self.pet.on_click(x, y)

    def _cat_hit_rect(self) -> QRect:
        """Tight bounding box of the cat's current sprite, in global screen coords
        (not the whole padded overlay window — that was swallowing nearby clicks)."""
        pm = _SPRITES.get(f'walk{self.pet.frame % 3}', _SPRITES['rest']) \
             if self.pet.moving else _SPRITES['rest']
        pad = 6
        w, h = pm.width() + pad * 2, pm.height() + pad * 2
        return QRect(int(self.pet.x - w / 2), int(self.pet.y - h), w, h)

    def _sync_bounds(self):
        r = active_window_rect()
        if r and r.width() > 80 and r.height() > 80:
            self.pet.set_bounds(r)
            self._home.update_pos(r)
            self._food_btn.update_pos(r)

    def _tick(self):
        now = time.monotonic()
        dt  = min(now - self._t, 0.05)
        self._t = now

        with self._lock:
            click, self._click = self._click, None
            keys,  self._keys  = self._keys, 0
        if click and not self._going_home:
            # clicking the cat itself toggles work/slack instead of moving it
            if self._cat_hit_rect().contains(QPoint(int(click[0]), int(click[1]))):
                self.pet.toggle_work()
            else:
                self.pet.on_click(*click)
        if keys:
            self.pet.add_keystrokes(keys)

        self.pet.update(dt)
        self.move(int(self.pet.x) - self.W//2, int(self.pet.y) - self.H)
        self.update()
        self._food_btn.set_progress(self.pet.bowl_progress, self.pet.bowls,
                                     self.pet.progress_keys, Pet.KEYS_PER_BOWL,
                                     int(self.pet.hunger))

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
        cat_top = draw_cat(p, self.W, self.H, self.pet.frame, self.pet.moving, self.pet.facing_right)
        bar_top = draw_hunger_bar(p, self.W, self.pet.hunger, cat_top)
        draw_speech_bubble(p, self.W, self.pet.speech, bar_top)
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
