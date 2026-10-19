"""Kumanda sunucusu (Windows). Telefondan gelen fare/klavye olaylarını PC'ye uygular.

1:1 Native çözünürlükte (BitBlt) keskin görüntü, GDI+ yüksek kaliteli JPEG akışı
ve PC'de seçilen metni (kullanıcı panosunu bozmadan) telefona aktarma desteği sağlar.
"""
import base64
import ctypes
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("KUMANDA_DATA") or os.path.join(HERE, "data")
PORT = 8765
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
gdiplus = ctypes.windll.gdiplus
ole32 = ctypes.windll.ole32
kernel32 = ctypes.windll.kernel32
ULONG_PTR = ctypes.c_size_t

kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.EmptyClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", POINT)]


# ---------------------------------------------------------------- GDI+ Yüksek Kaliteli JPEG Motoru
class GUID_STRUCT(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class GdiplusStartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32), ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", wintypes.BOOL), ("SuppressExternalCodecs", wintypes.BOOL)]


class EncoderParameter(ctypes.Structure):
    _fields_ = [("Guid", GUID_STRUCT), ("NumberOfValues", ctypes.c_ulong),
                ("Type", ctypes.c_ulong), ("Value", ctypes.c_void_p)]


class EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint), ("Parameter", EncoderParameter * 1)]


_gdip_token = ctypes.c_ulong()
_startup_in = GdiplusStartupInput(1, None, False, False)
gdiplus.GdiplusStartup(ctypes.byref(_gdip_token), ctypes.byref(_startup_in), None)

CLSID_JPEG = GUID_STRUCT(0x557CF401, 0x1A04, 0x11D3, (ctypes.c_ubyte * 8)(0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E))
GUID_QUALITY = GUID_STRUCT(0x1D5BE4B5, 0xFA4A, 0x452D, (ctypes.c_ubyte * 8)(0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB))

_cap_lock = threading.Lock()
_cached_frame = b""
_cached_time = 0.0


def capture_screen_jpeg(scale=1.0, quality=88):
    """Ekranı 1:1 (BitBlt) veya HALFTONE ile pikselleştirmeden cam gibi net yakalar."""
    global _cached_frame, _cached_time
    now = time.monotonic()
    if _cached_frame and (now - _cached_time) < 0.016:
        return _cached_frame

    with _cap_lock:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        tw = max(640, int(sw * scale))
        th = max(360, int(sh * scale))

        hdc_scr = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_scr)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_scr, tw, th)
        old = gdi32.SelectObject(hdc_mem, hbmp)

        if abs(scale - 1.0) < 0.02:
            gdi32.BitBlt(hdc_mem, 0, 0, tw, th, hdc_scr, 0, 0, 0x00CC0020)
        else:
            gdi32.SetStretchBltMode(hdc_mem, 4)
            gdi32.SetBrushOrgEx(hdc_mem, 0, 0, None)
            gdi32.StretchBlt(hdc_mem, 0, 0, tw, th, hdc_scr, 0, 0, sw, sh, 0x00CC0020)

        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if user32.GetCursorInfo(ctypes.byref(ci)) and (ci.flags & 1):
            cx = int(ci.ptScreenPos.x * scale)
            cy = int(ci.ptScreenPos.y * scale)
            user32.DrawIconEx(hdc_mem, cx, cy, ci.hCursor, 24, 24, 0, 0, 3)

        gp_bmp = ctypes.c_void_p()
        gdiplus.GdipCreateBitmapFromHBITMAP(hbmp, 0, ctypes.byref(gp_bmp))

        stream = ctypes.c_void_p()
        ole32.CreateStreamOnHGlobal(0, True, ctypes.byref(stream))

        q_val = ctypes.c_ulong(max(40, min(98, int(quality))))
        ep = EncoderParameters()
        ep.Count = 1
        ep.Parameter[0].Guid = GUID_QUALITY
        ep.Parameter[0].NumberOfValues = 1
        ep.Parameter[0].Type = 4
        ep.Parameter[0].Value = ctypes.cast(ctypes.byref(q_val), ctypes.c_void_p)

        gdiplus.GdipSaveImageToStream(gp_bmp, stream, ctypes.byref(CLSID_JPEG), ctypes.byref(ep))

        hglob = wintypes.HGLOBAL()
        ole32.GetHGlobalFromStream(stream, ctypes.byref(hglob))
        size = kernel32.GlobalSize(hglob)
        ptr = kernel32.GlobalLock(hglob)
        jpg_bytes = ctypes.string_at(ptr, size) if (ptr and size) else b""
        kernel32.GlobalUnlock(hglob)

        vtable = ctypes.cast(stream, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        release_fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        release_fn(stream)
        gdiplus.GdipDisposeImage(gp_bmp)

        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_scr)

        _cached_frame = jpg_bytes
        _cached_time = time.monotonic()
        return jpg_bytes


# ---------------------------------------------------------------- Pano (Clipboard) & Seçili Metin Yakalama
_clip_lock = threading.Lock()


def get_clipboard_text():
    text = ""
    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                h = user32.GetClipboardData(CF_UNICODETEXT)
                if h:
                    p = kernel32.GlobalLock(h)
                    if p:
                        try:
                            text = ctypes.wstring_at(p)
                        finally:
                            kernel32.GlobalUnlock(h)
            finally:
                user32.CloseClipboard()
            break
        time.sleep(0.01)
    return text


def set_clipboard_text(text):
    for _ in range(5):
        if user32.OpenClipboard(0):
            try:
                user32.EmptyClipboard()
                if text:
                    raw = (text + "\0").encode("utf-16-le")
                    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
                    if h:
                        p = kernel32.GlobalLock(h)
                        if p:
                            ctypes.memmove(p, raw, len(raw))
                            kernel32.GlobalUnlock(h)
                            user32.SetClipboardData(CF_UNICODETEXT, h)
            finally:
                user32.CloseClipboard()
            break
        time.sleep(0.01)


def send(*items):
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


MOVE, LDOWN, LUP, RDOWN, RUP, MDOWN, MUP, WHEEL, HWHEEL, ABSOLUTE = (
    0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x800, 0x1000, 0x8000
)
BTN = {"l": (LDOWN, LUP), "r": (RDOWN, RUP), "m": (MDOWN, MUP)}

VK = {"enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20, "delete": 0x2E,
      "insert": 0x2D, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24,
      "end": 0x23, "pgup": 0x21, "pgdn": 0x22, "prtsc": 0x2C, "win": 0x5B,
      "ctrl": 0x11, "alt": 0x12, "shift": 0x10}
for _i in range(1, 13):
    VK["f%d" % _i] = 0x6F + _i
EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
MODS = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}


def mouse(flags, dx=0, dy=0, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def abs_xy(rx, ry):
    rx = max(0.0, min(1.0, float(rx)))
    ry = max(0.0, min(1.0, float(ry)))
    return int(rx * 65535), int(ry * 65535)


def key(vk, up=False):
    i = INPUT(type=1)
    flags = (2 if up else 0) | (1 if vk in EXT else 0)
    i.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    return i


def uni(code, up=False):
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(0, code, 4 | (2 if up else 0), 0, 0)
    return i


def type_text(text):
    for ch in text:
        if ch == "\n":
            send(key(VK["enter"]), key(VK["enter"], True))
            continue
        if ch == "\t":
            send(key(VK["tab"]), key(VK["tab"], True))
            continue
        raw = ch.encode("utf-16-le")
        for n in range(0, len(raw), 2):
            unit = int.from_bytes(raw[n:n + 2], "little")
            send(uni(unit), uni(unit, True))


def vk_for(k):
    if k in VK:
        return VK[k]
    if len(k) == 1 and k.isascii() and k.isalnum():
        return ord(k.upper())
    return None


def press(k, mods):
    code = vk_for(k)
    if code is None:
        return
    mv = [MODS[m] for m in mods if m in MODS]
    send(*([key(v) for v in mv] + [key(code), key(code, True)] + [key(v, True) for v in reversed(mv)]))


def clamp(v, lim=3000):
    return max(-lim, min(lim, int(v)))


def probe_selected_text(send_ws_fn):
    """PC'de seçili metin olup olmadığını panoyu bozmadan kontrol eder ve telefona gönderir."""
    def _worker():
        with _clip_lock:
            time.sleep(0.04)
            old_text = get_clipboard_text()
            set_clipboard_text("")
            seq0 = user32.GetClipboardSequenceNumber()
            press("c", ["ctrl"])
            sel_text = ""
            for _ in range(12):
                time.sleep(0.015)
                if user32.GetClipboardSequenceNumber() != seq0:
                    sel_text = get_clipboard_text()
                    break
            # Kullanıcının eski panosunu geri yükle
            if old_text:
                set_clipboard_text(old_text)
            send_ws_fn({"t": "sel", "s": sel_text.strip() if sel_text else ""})

    threading.Thread(target=_worker, daemon=True).start()


def handle(ev, send_ws_fn=None):
    t = ev.get("t")
    if t == "move":
        send(mouse(MOVE, clamp(ev.get("dx", 0)), clamp(ev.get("dy", 0))))
    elif t == "abs_move":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        send(mouse(MOVE | ABSOLUTE, ax, ay))
    elif t == "abs_click":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        b = ev.get("b", "l")
        d, u = BTN.get(b, BTN["l"])
        send(mouse(MOVE | ABSOLUTE, ax, ay), mouse(d), mouse(u))
        # Sol tıklama genellikle seçimi kaldırır, kontrol et
        if b == "l" and send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "abs_dclick":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        d, u = BTN["l"]
        send(mouse(MOVE | ABSOLUTE, ax, ay), mouse(d), mouse(u), mouse(d), mouse(u))
        # Çift tıklama kelime seçer, yakala ve telefona gönder
        if send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "abs_down":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        d, _ = BTN.get(ev.get("b", "l"), BTN["l"])
        send(mouse(MOVE | ABSOLUTE, ax, ay), mouse(d))
    elif t == "abs_up":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        b = ev.get("b", "l")
        _, u = BTN.get(b, BTN["l"])
        send(mouse(MOVE | ABSOLUTE, ax, ay), mouse(u))
        # Sürükleme bırakıldığında metin seçilmiş olabilir, yakala ve telefona gönder
        if b == "l" and send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "probe_sel":
        if send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "click":
        b = ev.get("b", "l")
        d, u = BTN.get(b, BTN["l"])
        send(mouse(d), mouse(u))
        if b == "l" and send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "dclick":
        d, u = BTN["l"]
        send(mouse(d), mouse(u), mouse(d), mouse(u))
        if send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "down":
        send(mouse(BTN.get(ev.get("b", "l"), BTN["l"])[0]))
    elif t == "up":
        b = ev.get("b", "l")
        send(mouse(BTN.get(b, BTN["l"])[1]))
        if b == "l" and send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "scroll":
        if ev.get("dy"):
            send(mouse(WHEEL, data=clamp(ev["dy"], 1200)))
        if ev.get("dx"):
            send(mouse(HWHEEL, data=clamp(ev["dx"], 1200)))
    elif t == "text":
        type_text(str(ev.get("s", ""))[:3000])
    elif t == "key":
        n = max(1, min(100, int(ev.get("n", 1) or 1)))
        k = str(ev.get("k", "")).lower()
        m = [x for x in ev.get("m", []) if x in MODS]
        for _ in range(n):
            press(k, m)


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_pin():
    p = os.path.join(DATA, "pin.txt")
    try:
        with open(p, encoding="utf-8") as f:
            pin = f.read().strip()
            if pin:
                return pin
    except OSError:
        pass
    pin = str(random.SystemRandom().randint(100000, 999999))
    with open(p, "w", encoding="utf-8") as f:
        f.write(pin)
    return pin


def ensure_cert(ip):
    cp, kp = os.path.join(DATA, "cert.pem"), os.path.join(DATA, "key.pem")
    if os.path.exists(cp) and os.path.exists(kp):
        return cp, kp
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kumanda")])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address(ip)),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(san, critical=False).sign(key, hashes.SHA256()))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cp, kp


PIN = ""

ICON_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
<rect width="192" height="192" rx="38" fill="#0f1115"/>
<rect x="22" y="34" width="148" height="96" rx="12" fill="#1a1d24" stroke="#4da3ff" stroke-width="8"/>
<circle cx="96" cy="82" r="22" fill="#4da3ff"/>
<path d="M64 154h64" stroke="#4da3ff" stroke-width="10" stroke-linecap="round"/>
</svg>"""


def read_exact(rfile, n):
    data = rfile.read(n)
    if data is None or len(data) < n:
        raise ConnectionError("closed")
    return data


def ws_read(rfile):
    b1, b2 = read_exact(rfile, 2)
    opcode, masked, ln = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", read_exact(rfile, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", read_exact(rfile, 8))[0]
    mask = read_exact(rfile, 4) if masked else b""
    payload = read_exact(rfile, ln) if ln else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def ws_pack(obj):
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ln = len(raw)
    if ln < 126:
        hdr = bytes([0x81, ln])
    elif ln < 65536:
        hdr = struct.pack(">BBH", 0x81, 126, ln)
    else:
        hdr = struct.pack(">BBQ", 0x81, 127, ln)
    return hdr + raw


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, OSError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            pin = qs.get("p", [""])[0]
            if not hmac.compare_digest(pin.encode(), PIN.encode()):
                self.reply(403, b"PIN yanlis")
                return
            self.websocket()
            return
        if u.path == "/stream.mjpg":
            pin = qs.get("p", [""])[0]
            if not hmac.compare_digest(pin.encode(), PIN.encode()):
                self.reply(403, b"PIN yanlis")
                return
            sc = max(0.5, min(1.0, float(qs.get("s", ["1.0"])[0])))
            ql = max(50, min(95, int(qs.get("q", ["88"])[0])))
            fps_delay = max(0.025, min(0.5, int(qs.get("d", ["45"])[0]) / 1000.0))
            self.mjpeg_stream(sc, ql, fps_delay)
            return
        if u.path in ("/screen.jpg", "/screen.bmp"):
            pin = qs.get("p", [""])[0]
            if not hmac.compare_digest(pin.encode(), PIN.encode()):
                self.reply(403, b"PIN yanlis")
                return
            try:
                sc = max(0.5, min(1.0, float(qs.get("s", ["1.0"])[0])))
                ql = max(50, min(95, int(qs.get("q", ["88"])[0])))
                jpg = capture_screen_jpeg(sc, ql)
                self.reply(200, jpg, "image/jpeg")
            except Exception as e:
                self.reply(500, str(e).encode("utf-8"))
            return
        if u.path == "/manifest.json":
            manifest = json.dumps({
                "name": "Kumanda PC Ekran",
                "short_name": "Kumanda",
                "start_url": "/?p=" + PIN,
                "display": "fullscreen",
                "orientation": "any",
                "background_color": "#000000",
                "theme_color": "#000000",
                "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]
            }).encode("utf-8")
            self.reply(200, manifest, "application/manifest+json; charset=utf-8")
            return
        if u.path == "/icon.svg":
            self.reply(200, ICON_SVG, "image/svg+xml")
            return
        if u.path == "/sw.js":
            sw_code = b"self.addEventListener('fetch', function(e){});"
            self.reply(200, sw_code, "application/javascript; charset=utf-8")
            return
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    self.reply(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self.reply(500, b"web/index.html yok")
            return
        self.reply(404, b"yok")

    def mjpeg_stream(self, scale, quality, delay):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            while True:
                t0 = time.monotonic()
                jpg = capture_screen_jpeg(scale, quality)
                if jpg:
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg)).encode("ascii") + b"\r\n\r\n"
                    )
                    self.wfile.write(header + jpg + b"\r\n")
                    self.wfile.flush()
                elapsed = time.monotonic() - t0
                rem = delay - elapsed
                if rem > 0.002:
                    time.sleep(rem)
        except (ConnectionError, OSError, ssl.SSLError):
            pass

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        wlock = threading.Lock()

        def send_ws(obj):
            with wlock:
                try:
                    self.wfile.write(ws_pack(obj))
                    self.wfile.flush()
                except Exception:
                    pass

        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            while True:
                op, payload = ws_read(rfile=self.rfile)
                if op == 8:
                    break
                if op == 9:
                    with wlock:
                        self.wfile.write(bytes([0x8A, len(payload)]) + payload)
                        self.wfile.flush()
                elif op == 1:
                    try:
                        handle(json.loads(payload.decode("utf-8")), send_ws)
                    except Exception as e:
                        print("olay hatası:", e)
        except (ConnectionError, OSError, ssl.SSLError):
            pass


def main():
    global PIN
    os.makedirs(DATA, exist_ok=True)
    PIN = get_pin()
    ip = local_ip()
    httpd = QuietServer(("0.0.0.0", PORT), H)
    scheme = "http"
    try:
        cp, kp = ensure_cert(ip)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cp, kp)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    except Exception as e:
        print("HTTPS açılamadı (%s). HTTP ile devam ediliyor, sesli yazma çalışmayabilir." % e)
    print("\n" + "=" * 60)
    print(" KUMANDA HAZIR (1:1 HD + Seçili Metin Yakalama Destekli)")
    print("=" * 60)
    print(" Telefonda (aynı Wi-Fi) şu adresi aç:\n")
    print("     %s://%s:%d/?p=%s\n" % (scheme, ip, PORT, PIN))
    print(" PIN: %s" % PIN)
    print("=" * 60 + "\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()