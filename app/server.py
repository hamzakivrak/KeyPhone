"""Kumanda sunucusu (Windows). Telefondan gelen fare/klavye ve çoklu oyun kolu (Gamepad) olaylarını PC'ye uygular.

1:1 Native çözünürlükte (BitBlt) keskin görüntü, GDI+ yüksek kaliteli JPEG akışı,
PC'de seçilen metni telefona aktarma, seçilen ekran bölgesini kırpma (/crop.jpg) ve
4 oyuncuya kadar Çoklu Oyun Kolu (Sanal Xbox 360 XInput + Donanım ScanCode Klavye/Fare + Direksiyon) desteği sağlar.
"""
import base64
import ctypes
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import os
import random
import socket
import ssl
import struct
import threading
import time
import urllib.request
import zipfile
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
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

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


def _hbitmap_to_jpeg(hbmp, quality=88):
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
    return jpg_bytes


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

        jpg_bytes = _hbitmap_to_jpeg(hbmp, quality)

        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_scr)

        _cached_frame = jpg_bytes
        _cached_time = time.monotonic()
        return jpg_bytes


def capture_crop_jpeg(x1=0.0, y1=0.0, x2=1.0, y2=1.0, quality=92):
    """OCR için ekranda seçilen dikdörtgen alanı (veya tam ekranı) imleçsiz ve yüksek netlikte kırpar."""
    with _cap_lock:
        sw = user32.GetSystemMetrics(0)
        sh = user32.GetSystemMetrics(1)
        rx1, rx2 = sorted((max(0.0, min(1.0, float(x1))), max(0.0, min(1.0, float(x2)))))
        ry1, ry2 = sorted((max(0.0, min(1.0, float(y1))), max(0.0, min(1.0, float(y2)))))

        if (rx2 - rx1) < 0.04 or (ry2 - ry1) < 0.03:
            rx1, ry1, rx2, ry2 = 0.0, 0.0, 1.0, 1.0

        px = int(rx1 * sw)
        py = int(ry1 * sh)
        pw = max(64, int((rx2 - rx1) * sw))
        ph = max(40, int((ry2 - ry1) * sh))

        hdc_scr = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_scr)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_scr, pw, ph)
        old = gdi32.SelectObject(hdc_mem, hbmp)

        gdi32.BitBlt(hdc_mem, 0, 0, pw, ph, hdc_scr, px, py, 0x00CC0020)
        jpg_bytes = _hbitmap_to_jpeg(hbmp, quality)

        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_scr)
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
    if not items:
        return
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


MOVE, LDOWN, LUP, RDOWN, RUP, MDOWN, MUP, WHEEL, HWHEEL, ABSOLUTE = (
    0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x800, 0x1000, 0x8000
)
BTN = {"l": (LDOWN, LUP), "r": (RDOWN, RUP), "m": (MDOWN, MUP)}

VK = {
    "enter": 0x0D, "backspace": 0x08, "tab": 0x09, "esc": 0x1B, "space": 0x20, "delete": 0x2E,
    "insert": 0x2D, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24,
    "end": 0x23, "pgup": 0x21, "pgdn": 0x22, "prtsc": 0x2C, "win": 0x5B,
    "ctrl": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "capslock": 0x14,
    "num0": 0x60, "num1": 0x61, "num2": 0x62, "num3": 0x63, "num4": 0x64,
    "num5": 0x65, "num6": 0x66, "num7": 0x67, "num8": 0x68, "num9": 0x69,
    "num_mul": 0x6A, "num_add": 0x6B, "num_sub": 0x6D, "num_dec": 0x6E, "num_div": 0x6F,
    "semicolon": 0xBA, "plus": 0xBB, "comma": 0xBC, "minus": 0xBD, "period": 0xBE, "slash": 0xBF,
    "tilde": 0xC0, "lbracket": 0xDB, "backslash": 0xDC, "rbracket": 0xDD, "quote": 0xDE
}
for _i in range(1, 13):
    VK["f%d" % _i] = 0x6F + _i
EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0xA3, 0xA5, 0x6F}
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
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    flags = (2 if up else 0) | (1 if vk in EXT else 0)
    i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


def game_key(vk, up=False):
    """DirectX / Vulkan / OpenGL oyunları için donanım ScanCode öncelikli tuş olayı üretir."""
    i = INPUT(type=1)
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFFFF
    if scan:
        flags = 0x0008 | (2 if up else 0) | (1 if vk in EXT else 0)
        i.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    else:
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
    if not k:
        return None
    k = str(k).lower().strip()
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


# ================================================================
# ÇOKLU OYUN KOLU (SANAL XBOX 360 KOLU - ViGEmBus & KLAVYE/FARE MOTORU)
# ================================================================
class XUSB_REPORT(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_uint16),
        ("bLeftTrigger", ctypes.c_uint8),
        ("bRightTrigger", ctypes.c_uint8),
        ("sThumbLX", ctypes.c_int16),
        ("sThumbLY", ctypes.c_int16),
        ("sThumbRX", ctypes.c_int16),
        ("sThumbRY", ctypes.c_int16),
    ]


class VirtualGamepadHub:
    """4 oyuncuya kadar Sanal Xbox 360 kolu (ViGEmBus) ve oyuncu slotlarını yönetir."""

    def __init__(self):
        self.lock = threading.Lock()
        self.dll = None
        self.client = None
        self.vigem_ready = False
        self.targets = {}  # slot (1..4) -> PVIGEM_TARGET
        self.slot_owners = {}  # slot (1..4) -> client_id
        self._init_attempted = False
        self._last_attempt = 0.0
        self.last_error = ""  # 'dll' | 'bus' | 'hata:<kod>' | ''

    def _find_or_download_dll(self):
        arch = "x64" if struct.calcsize("P") * 8 == 64 else "x86"
        candidates = [
            os.path.join(HERE, "ViGEmClient.dll"),
            os.path.join(DATA, f"ViGEmClient_{arch}.dll"),
            os.path.join(DATA, "ViGEmClient.dll"),
        ]
        # Eğer vgamepad kuruluysa onun içindeki DLL'i bul
        try:
            import vgamepad  # type: ignore
            vg_dir = os.path.dirname(vgamepad.__file__)
            for root, _, files in os.walk(vg_dir):
                for f in files:
                    if f.lower() == "vigemclient.dll" and (arch in root.lower() or "64" in root.lower() or arch == "x86"):
                        candidates.insert(0, os.path.join(root, f))
        except Exception:
            pass

        for p in candidates:
            if os.path.exists(p):
                return p

        # Yoksa PyPI üzerindeki resmi vgamepad paketinden ViGEmClient.dll'i DATA klasörüne indir
        target_dll = os.path.join(DATA, f"ViGEmClient_{arch}.dll")
        try:
            os.makedirs(DATA, exist_ok=True)
            req = urllib.request.Request(
                "https://pypi.org/pypi/vgamepad/json",
                headers={"User-Agent": "Kumanda-Gamepad/1.0"},
            )
            meta = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
            urls = meta.get("urls", [])
            whl_url = ""
            for u in urls:
                fn = u.get("filename", "")
                if fn.endswith(".whl") and ("win_amd64" in fn if arch == "x64" else "win32" in fn):
                    whl_url = u.get("url", "")
                    break
            if not whl_url and urls:
                for u in urls:
                    if u.get("filename", "").endswith(".whl"):
                        whl_url = u.get("url", "")
                        break
            if whl_url:
                whl_bytes = urllib.request.urlopen(whl_url, timeout=8).read()
                with zipfile.ZipFile(io.BytesIO(whl_bytes)) as z:
                    for name in z.namelist():
                        if name.lower().endswith("vigemclient.dll") and (arch in name.lower() or "x64" in name.lower() or "amd64" in name.lower()):
                            with open(target_dll, "wb") as f:
                                f.write(z.read(name))
                            return target_dll
        except Exception:
            pass
        return None

    def ensure_initialized(self, retry=False):
        """ViGEm'i hazırlar. retry=True ise (ör. telefon yeniden bağlandığında) ViGEmBus sonradan
        kurulmuş olabilir diye en fazla 3 saniyede bir yeniden dener."""
        with self.lock:
            if self.vigem_ready:
                return True
            now = time.monotonic()
            if self._init_attempted and not (retry and now - self._last_attempt > 3.0):
                return False
            self._init_attempted = True
            self._last_attempt = now

            dll_path = self._find_or_download_dll()
            if not dll_path:
                self.last_error = "dll"
                print(" [Gamepad] ViGEmClient.dll bulunamadı/indirilemedi (internet gerekli).")
                return False
            try:
                dll = ctypes.WinDLL(dll_path)
                dll.vigem_alloc.restype = ctypes.c_void_p
                dll.vigem_free.argtypes = [ctypes.c_void_p]
                dll.vigem_connect.argtypes = [ctypes.c_void_p]
                dll.vigem_connect.restype = ctypes.c_uint32
                dll.vigem_disconnect.argtypes = [ctypes.c_void_p]
                dll.vigem_target_x360_alloc.restype = ctypes.c_void_p
                dll.vigem_target_free.argtypes = [ctypes.c_void_p]
                dll.vigem_target_add.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                dll.vigem_target_add.restype = ctypes.c_uint32
                dll.vigem_target_remove.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                dll.vigem_target_remove.restype = ctypes.c_uint32
                dll.vigem_target_x360_update.argtypes = [ctypes.c_void_p, ctypes.c_void_p, XUSB_REPORT]
                dll.vigem_target_x360_update.restype = ctypes.c_uint32

                client = dll.vigem_alloc()
                if not client:
                    self.last_error = "hata:alloc"
                    return False
                err = dll.vigem_connect(client)
                if err != 0x20000000:  # VIGEM_ERROR_NONE
                    dll.vigem_free(client)
                    # 0xE0000001 = VIGEM_ERROR_BUS_NOT_FOUND: ViGEmBus sürücüsü PC'de kurulu değil
                    self.last_error = "bus" if err == 0xE0000001 else "hata:%08X" % err
                    print(" [Gamepad] ViGEmBus sürücüsü bulunamadı (kod %08X). "
                          "Sanal Xbox kolu için ViGEmBus kurulmalı; şimdilik Klavye/Fare kullanılıyor." % err)
                    return False

                self.dll = dll
                self.client = client
                self.vigem_ready = True
                self.last_error = ""
                print(" [Gamepad] ViGEmBus bulundu: Sanal Xbox 360 Kol desteği AKTİF.")
                return True
            except Exception as e:
                self.last_error = "hata:%s" % e
                print(" [Gamepad] ViGEmBus başlatılamadı, Klavye/Fare emülasyonu kullanılacak:", e)
                return False

    def assign_slot(self, client_id, preferred_slot=None):
        with self.lock:
            # Mevcut slotu varsa ve tercih belirtilmediyse koru
            for s, cid in list(self.slot_owners.items()):
                if cid == client_id:
                    if preferred_slot is None or preferred_slot == s:
                        return s
                    del self.slot_owners[s]
                    self._drop_target_locked(s)  # eski slotta hayalet sanal kol kalmasın

            if preferred_slot in (1, 2, 3, 4) and preferred_slot not in self.slot_owners:
                self.slot_owners[preferred_slot] = client_id
                return preferred_slot

            for s in (1, 2, 3, 4):
                if s not in self.slot_owners:
                    self.slot_owners[s] = client_id
                    return s

            # Tüm slotlar doluysa tercih edileni veya 1'i ver
            s = preferred_slot if preferred_slot in (1, 2, 3, 4) else 1
            self.slot_owners[s] = client_id
            return s

    def _drop_target_locked(self, slot):
        """Kilit zaten alınmışken: slota ait sanal Xbox kolunu Windows'tan kaldırır."""
        target = self.targets.pop(slot, None)
        if target and self.vigem_ready:
            try:
                self.dll.vigem_target_x360_update(self.client, target, XUSB_REPORT(0, 0, 0, 0, 0, 0, 0))
                self.dll.vigem_target_remove(self.client, target)
                self.dll.vigem_target_free(target)
            except Exception:
                pass

    def _get_or_create_target(self, slot):
        if not self.vigem_ready:
            return None
        t = self.targets.get(slot)
        if t:
            return t
        t = self.dll.vigem_target_x360_alloc()
        if not t:
            return None
        err = self.dll.vigem_target_add(self.client, t)
        if err != 0x20000000:
            self.dll.vigem_target_free(t)
            return None
        self.targets[slot] = t
        return t

    def update_x360(self, slot, btns=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0):
        if not self.vigem_ready and not self._init_attempted:
            self.ensure_initialized()
        if not self.vigem_ready:
            return False
        with self.lock:
            target = self._get_or_create_target(slot)
            if not target:
                return False
            rep = XUSB_REPORT(
                int(btns) & 0xFFFF,
                max(0, min(255, int(lt))),
                max(0, min(255, int(rt))),
                max(-32768, min(32767, int(lx))),
                max(-32768, min(32767, int(ly))),
                max(-32768, min(32767, int(rx))),
                max(-32768, min(32767, int(ry))),
            )
            err = self.dll.vigem_target_x360_update(self.client, target, rep)
            return err == 0x20000000

    def release_client(self, client_id):
        with self.lock:
            freed_slots = [s for s, cid in self.slot_owners.items() if cid == client_id]
            for s in freed_slots:
                del self.slot_owners[s]
                target = self.targets.pop(s, None)
                if target and self.vigem_ready:
                    try:
                        self.dll.vigem_target_x360_update(self.client, target, XUSB_REPORT(0, 0, 0, 0, 0, 0, 0))
                        self.dll.vigem_target_remove(self.client, target)
                        self.dll.vigem_target_free(target)
                    except Exception:
                        pass

    def active_slots_list(self):
        with self.lock:
            return sorted(list(self.slot_owners.keys()))


GAMEPAD_HUB = VirtualGamepadHub()

# Global referans sayacı: Birden fazla oyuncu aynı klavye tuşuna basarsa çakışmayı önler
_global_key_lock = threading.Lock()
_global_held_counts = {}  # vk -> count
_global_mouse_counts = {"l": 0, "r": 0, "m": 0}


def _acquire_vk(vk):
    with _global_key_lock:
        cnt = _global_held_counts.get(vk, 0)
        _global_held_counts[vk] = cnt + 1
        if cnt == 0:
            send(game_key(vk, False))


def _release_vk(vk):
    with _global_key_lock:
        cnt = _global_held_counts.get(vk, 0)
        if cnt <= 1:
            _global_held_counts.pop(vk, None)
            send(game_key(vk, True))
        else:
            _global_held_counts[vk] = cnt - 1


def _acquire_mb(b):
    if b not in BTN:
        return
    with _global_key_lock:
        cnt = _global_mouse_counts.get(b, 0)
        _global_mouse_counts[b] = cnt + 1
        if cnt == 0:
            send(mouse(BTN[b][0]))


def _release_mb(b):
    if b not in BTN:
        return
    with _global_key_lock:
        cnt = _global_mouse_counts.get(b, 0)
        if cnt <= 1:
            _global_mouse_counts[b] = 0
            send(mouse(BTN[b][1]))
        else:
            _global_mouse_counts[b] = cnt - 1


def sync_client_keys_and_mouse(client_state, desired_key_names, desired_mbs, mdx=0, mdy=0):
    """İstemcinin basılı tuttuğu oyun tuşlarını ve fare butonlarını önceki durumla fark (diff) alarak günceller."""
    if client_state is None:
        return
    new_vks = set()
    for kname in desired_key_names:
        vk = vk_for(kname)
        if vk is not None:
            new_vks.add(vk)

    old_vks = client_state["held_vks"]
    for vk in (old_vks - new_vks):
        _release_vk(vk)
    for vk in (new_vks - old_vks):
        _acquire_vk(vk)
    client_state["held_vks"] = new_vks

    new_mbs = set(b for b in desired_mbs if b in BTN)
    old_mbs = client_state["held_mbs"]
    for b in (old_mbs - new_mbs):
        _release_mb(b)
    for b in (new_mbs - old_mbs):
        _acquire_mb(b)
    client_state["held_mbs"] = new_mbs

    if mdx or mdy:
        send(mouse(MOVE, clamp(mdx, 1800), clamp(mdy, 1800)))


def release_client_state(client_state):
    """Bağlantı koptuğunda veya sekme değiştiğinde istemcinin basılı kalan tüm tuşlarını ve kolunu bırakır."""
    if not client_state:
        return
    for vk in list(client_state.get("held_vks", set())):
        _release_vk(vk)
    client_state["held_vks"].clear()

    for b in list(client_state.get("held_mbs", set())):
        _release_mb(b)
    client_state["held_mbs"].clear()

    cid = client_state.get("id")
    if cid:
        GAMEPAD_HUB.release_client(cid)


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
            if old_text:
                set_clipboard_text(old_text)
            send_ws_fn({"t": "sel", "s": sel_text.strip() if sel_text else ""})

    threading.Thread(target=_worker, daemon=True).start()


def handle(ev, send_ws_fn=None, client_state=None):
    t = ev.get("t")
    if t == "pad":
        # Yüksek hızlı birleşik Oyun Kolu paketi (XInput + Klavye/Fare Yedek veya Özel Profil)
        mode = ev.get("mode", "auto")  # 'auto' | 'xinput' | 'kb'
        # Slot yalnızca 'pad_init' ile değişir. Paketlerdeki slot değeri yok sayılır;
        # böylece iki cihaz aynı "P1" tercihini gönderse bile birbirinin kolunu ezmez.
        slot = client_state.get("slot", 1) if client_state else int(ev.get("slot") or 1)

        used_xinput = False
        if mode in ("auto", "xinput"):
            used_xinput = GAMEPAD_HUB.update_x360(
                slot=slot,
                btns=ev.get("btns", 0),
                lt=ev.get("lt", 0),
                rt=ev.get("rt", 0),
                lx=ev.get("lx", 0),
                ly=ev.get("ly", 0),
                rx=ev.get("rx", 0),
                ry=ev.get("ry", 0),
            )

        # Klavye/Fare modu seçiliyse VEYA XInput sürücüsü yoksa (ya da FPS fare bakışı varsa) klavye/fare uygula
        if mode == "kb" or not used_xinput:
            sync_client_keys_and_mouse(
                client_state,
                ev.get("keys", []),
                ev.get("mb", []),
                ev.get("mdx", 0),
                ev.get("mdy", 0),
            )
        else:
            # XInput aktifken bile sağ çubuk 'fare bakışı' (mdx/mdy) gönderiyorsa uygula, basılı klavye tuşu varsa temizle
            if client_state and (client_state["held_vks"] or client_state["held_mbs"]):
                sync_client_keys_and_mouse(client_state, [], [], 0, 0)
            mdx, mdy = ev.get("mdx", 0), ev.get("mdy", 0)
            if mdx or mdy:
                send(mouse(MOVE, clamp(mdx, 1800), clamp(mdy, 1800)))
        return

    if t == "pad_init":
        if client_state:
            pref = ev.get("slot")
            pref = int(pref) if pref in (1, 2, 3, 4, "1", "2", "3", "4") else None
            slot = GAMEPAD_HUB.assign_slot(client_state["id"], pref)
            client_state["slot"] = slot
            vigem_ok = GAMEPAD_HUB.ensure_initialized(retry=True)
            if vigem_ok:
                GAMEPAD_HUB.update_x360(slot, 0, 0, 0, 0, 0, 0, 0)
            if send_ws_fn:
                send_ws_fn({
                    "t": "pad_info",
                    "slot": slot,
                    "wanted": pref,
                    "vigem": vigem_ok,
                    "reason": "" if vigem_ok else GAMEPAD_HUB.last_error,
                    "active_slots": GAMEPAD_HUB.active_slots_list(),
                })
        return

    if t == "pad_release":
        if client_state:
            sync_client_keys_and_mouse(client_state, [], [], 0, 0)
            slot = client_state.get("slot", 1)
            if GAMEPAD_HUB.vigem_ready:
                GAMEPAD_HUB.update_x360(slot, 0, 0, 0, 0, 0, 0, 0)
        return

    if t == "keydown":
        vk = vk_for(ev.get("k", ""))
        if vk is not None and client_state is not None:
            if vk not in client_state["held_vks"]:
                client_state["held_vks"].add(vk)
                _acquire_vk(vk)
        elif vk is not None:
            send(game_key(vk, False))
        return

    if t == "keyup":
        vk = vk_for(ev.get("k", ""))
        if vk is not None and client_state is not None:
            if vk in client_state["held_vks"]:
                client_state["held_vks"].remove(vk)
                _release_vk(vk)
        elif vk is not None:
            send(game_key(vk, True))
        return

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
        if b == "l" and send_ws_fn:
            probe_selected_text(send_ws_fn)
    elif t == "abs_dclick":
        ax, ay = abs_xy(ev.get("x", 0), ev.get("y", 0))
        d, u = BTN["l"]
        send(mouse(MOVE | ABSOLUTE, ax, ay), mouse(d), mouse(u), mouse(d), mouse(u))
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


_client_seq = 0
_client_seq_lock = threading.Lock()


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
        if u.path == "/crop.jpg":
            pin = qs.get("p", [""])[0]
            if not hmac.compare_digest(pin.encode(), PIN.encode()):
                self.reply(403, b"PIN yanlis")
                return
            try:
                x1 = float(qs.get("x1", ["0"])[0])
                y1 = float(qs.get("y1", ["0"])[0])
                x2 = float(qs.get("x2", ["1"])[0])
                y2 = float(qs.get("y2", ["1"])[0])
                jpg = capture_crop_jpeg(x1, y1, x2, y2, 92)
                self.reply(200, jpg, "image/jpeg")
            except Exception as e:
                self.reply(500, str(e).encode("utf-8"))
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
                "name": "Kumanda PC Ekran & Oyun Kolu",
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
        global _client_seq
        key_hdr = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key_hdr + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        wlock = threading.Lock()

        with _client_seq_lock:
            _client_seq += 1
            cid = _client_seq

        client_state = {
            "id": cid,
            "slot": 1,
            "held_vks": set(),
            "held_mbs": set(),
        }

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
                        handle(json.loads(payload.decode("utf-8")), send_ws, client_state)
                    except Exception as e:
                        print("olay hatası:", e)
        except (ConnectionError, OSError, ssl.SSLError):
            pass
        finally:
            release_client_state(client_state)


def main():
    global PIN
    os.makedirs(DATA, exist_ok=True)
    PIN = get_pin()
    ip = local_ip()

    # Arka planda sanal Xbox 360 gamepad sürücüsünü (ViGEmBus) hazırla
    threading.Thread(target=GAMEPAD_HUB.ensure_initialized, daemon=True).start()

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
    print("\n" + "=" * 64)
    print(" KUMANDA HAZIR (1:1 HD + AI/OCR + 4 Kişilik Oyun Kolu Destekli)")
    print("=" * 64)
    print(" Telefonda veya Tablette (aynı Wi-Fi) şu adresi aç:\n")
    print("     %s://%s:%d/?p=%s\n" % (scheme, ip, PORT, PIN))
    print(" PIN: %s" % PIN)
    print("=" * 64 + "\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
