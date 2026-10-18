"""Kumanda sunucusu (Windows). Telefondan gelen fare/klavye olaylarını PC'ye uygular.

Telefon tarayıcıdan https://<pc-ip>:8765 adresini açar ve WebSocket ile bağlanır.
Yalnızca Python standart kütüphanesi + HTTPS sertifikası için 'cryptography' kullanır.
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
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("KUMANDA_DATA") or os.path.join(HERE, "data")
PORT = 8765
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# ---------------------------------------------------------------- Windows girdi
user32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_size_t


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


def send(*items):
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))


MOVE, LDOWN, LUP, RDOWN, RUP, MDOWN, MUP, WHEEL, HWHEEL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x800, 0x1000
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


def handle(ev):
    t = ev.get("t")
    if t == "move":
        send(mouse(MOVE, clamp(ev.get("dx", 0)), clamp(ev.get("dy", 0))))
    elif t == "click":
        d, u = BTN.get(ev.get("b", "l"), BTN["l"])
        send(mouse(d), mouse(u))
    elif t == "dclick":
        d, u = BTN["l"]
        send(mouse(d), mouse(u), mouse(d), mouse(u))
    elif t == "down":
        send(mouse(BTN.get(ev.get("b", "l"), BTN["l"])[0]))
    elif t == "up":
        send(mouse(BTN.get(ev.get("b", "l"), BTN["l"])[1]))
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


# ---------------------------------------------------------------- Ağ / HTTPS / PIN
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


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Telefon bağlantıyı kapatınca / sertifika uyarısında oluşan gürültülü hataları gizle.
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
        if u.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            pin = parse_qs(u.query).get("p", [""])[0]
            if not hmac.compare_digest(pin.encode(), PIN.encode()):
                self.reply(403, b"PIN yanlis")
                return
            self.websocket()
            return
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "web", "index.html"), "rb") as f:
                    self.reply(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self.reply(500, b"web/index.html yok")
            return
        self.reply(404, b"yok")

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.close_connection = True
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            while True:
                op, payload = ws_read(self.rfile)
                if op == 8:
                    break
                if op == 9:  # ping -> pong
                    self.wfile.write(bytes([0x8A, len(payload)]) + payload)
                    self.wfile.flush()
                elif op == 1:
                    try:
                        handle(json.loads(payload.decode("utf-8")))
                    except Exception as e:  # tek bir hatalı mesaj bağlantıyı düşürmesin
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
    print(" KUMANDA HAZIR")
    print("=" * 60)
    print(" Telefonda (aynı Wi-Fi) şu adresi aç:\n")
    print("     %s://%s:%d/?p=%s\n" % (scheme, ip, PORT, PIN))
    print(" PIN: %s" % PIN)
    if scheme == "https":
        print(" İlk açılışta tarayıcı uyarı verir: 'Gelişmiş' > 'Devam et' de.")
    print(" Windows güvenlik duvarı sorarsa 'Özel ağlar'a izin ver.")
    print(" Kapatmak için bu pencereyi kapat.")
    print("=" * 60 + "\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
