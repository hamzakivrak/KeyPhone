"""Kumanda başlatıcı.

Açılırken GitHub'daki en son `app/` klasörünü indirir ve çalıştırır.
Bu dosya neredeyse hiç değişmez; asıl program app/server.py ve app/web/index.html'dedir.
"""
import os
import sys
import io
import shutil
import zipfile
import urllib.request
import runpy
import traceback

# Aşağıdaki modüller indirilen kod tarafından kullanılır. Exe'ye dahil edilmeleri için burada içe aktarılır.
import ssl, socket, json, hashlib, base64, struct, threading, ctypes, ctypes.wintypes  # noqa: E401,F401
import datetime, ipaddress, random, hmac, secrets, http.server, socketserver, time, urllib.parse, re  # noqa: E401,F401
try:
    import cryptography, cryptography.x509, cryptography.x509.oid  # noqa: F401
    from cryptography.hazmat.primitives import hashes, serialization  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: F401
except Exception:
    pass

DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Kumanda")


def exe_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def read_repo(ed):
    repo = os.environ.get("KUMANDA_REPO", "").strip()
    if not repo:
        try:
            with open(os.path.join(ed, "repo.txt"), encoding="utf-8") as f:
                repo = f.readline().strip()
        except OSError:
            repo = ""
    if "/" not in repo or repo.upper().startswith("KULLANICI"):
        return ""
    return repo


def update(repo, cache):
    data = None
    for branch in ("main", "master"):
        try:
            req = urllib.request.Request(
                f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}",
                headers={"User-Agent": "Kumanda"},
            )
            data = urllib.request.urlopen(req, timeout=12).read()
            break
        except Exception:
            data = None
    if not data:
        return False
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        tmp = cache + ".new"
        shutil.rmtree(tmp, ignore_errors=True)
        for name in z.namelist():
            parts = name.split("/", 1)
            if len(parts) < 2 or name.endswith("/"):
                continue
            rel = parts[1]
            if not rel.startswith("app/") or ".." in rel:
                continue
            out = os.path.join(tmp, rel[4:])
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(z.read(name))
        if os.path.exists(os.path.join(tmp, "server.py")):
            shutil.rmtree(cache, ignore_errors=True)
            os.replace(tmp, cache)
            return True
    except Exception as e:
        print("Güncelleme açılamadı:", e)
    return False


def run_app(appdir):
    os.environ["KUMANDA_DATA"] = DATA
    sys.path.insert(0, appdir)
    runpy.run_path(os.path.join(appdir, "server.py"), run_name="__main__")


def main():
    ed = exe_dir()
    os.makedirs(DATA, exist_ok=True)
    cache = os.path.join(DATA, "app")
    bundled = os.path.join(ed, "app")
    repo = read_repo(ed)

    print("Kumanda başlıyor...")
    if repo:
        print(f"Güncellemeler kontrol ediliyor ({repo})...")
        if update(repo, cache):
            print("En son sürüm indirildi.")
        else:
            print("GitHub'a ulaşılamadı, mevcut sürüm kullanılacak.")
    else:
        print("repo.txt ayarlı değil, güncelleme atlanıyor.")

    candidates = []
    if os.path.exists(os.path.join(cache, "server.py")):
        candidates.append(cache)
    if os.path.exists(os.path.join(bundled, "server.py")):
        candidates.append(bundled)
    if not candidates:
        print("HATA: app/server.py bulunamadı.")
        input("Kapatmak için Enter'a bas...")
        return

    for i, appdir in enumerate(candidates):
        try:
            run_app(appdir)
            return
        except (KeyboardInterrupt, SystemExit):
            return
        except Exception:
            traceback.print_exc()
            if i + 1 < len(candidates):
                print("\nİndirilen sürüm çalışmadı, pakette gelen sürüm deneniyor...\n")
    input("Hata oluştu. Kapatmak için Enter'a bas...")


if __name__ == "__main__":
    main()
