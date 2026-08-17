import os
import sys
import shutil
import urllib.request
import zipfile
import subprocess

ROOT = r"D:\Lin_Agent\WB-WorkSpace\2026-08-17-00-34-56\score-studio"
DIST = os.path.join(ROOT, "python_dist")
VERSION = "3.13.12"
EMBED_URL = f"https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip"
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"
TMP_ZIP = os.path.join(ROOT, "_embed.zip")
GETPIP = os.path.join(ROOT, "_getpip.py")

def run(cmd, cwd=None):
    print(">>", " ".join(cmd))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout[-2000:])
    if r.returncode != 0:
        print("STDERR:", r.stderr[-3000:])
        raise SystemExit(f"command failed: {cmd}")
    return r

def main():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)

    # 1. download embeddable
    print("== download embeddable ==")
    urllib.request.urlretrieve(EMBED_URL, TMP_ZIP)

    # 2. extract
    print("== extract ==")
    with zipfile.ZipFile(TMP_ZIP) as z:
        z.extractall(DIST)
    os.remove(TMP_ZIP)

    py = os.path.join(DIST, "python.exe")

    # 2.5 fix _pth (enable import site) BEFORE pip, so -m pip can find site-packages
    print("== fix _pth (early) ==")
    pth = os.path.join(DIST, "python313._pth")
    if os.path.exists(pth):
        with open(pth) as f:
            lines = f.read().splitlines()
        out = []
        for ln in lines:
            if ln.strip().lstrip("#").strip() == "import site":
                out.append("import site")
            else:
                out.append(ln)
        if not any(l.strip() == "import site" for l in out):
            out.append("import site")
        with open(pth, "w") as f:
            f.write("\n".join(out) + "\n")

    # 3. get-pip
    print("== download get-pip ==")
    urllib.request.urlretrieve(GETPIP_URL, GETPIP)
    run([py, GETPIP, "--no-warn-script-location"], cwd=DIST)
    os.remove(GETPIP)

    # 4. install deps
    print("== pip install deps ==")
    run([py, "-m", "pip", "install", "--no-cache-dir", "--no-warn-script-location",
         "Pillow", "numpy", "PyMuPDF"], cwd=DIST)

    # 6. copy pipeline script
    print("== copy pipeline ==")
    shutil.copy(os.path.join(ROOT, "sheet_pipeline.py"), DIST)

    # 7. test
    print("== test imports ==")
    r = subprocess.run([py, "-c", "import PIL, numpy, fitz; print('IMPORTS_OK', PIL.__version__, numpy.__version__, fitz.__doc__[:40])"],
                       capture_output=True, text=True)
    print(r.stdout, r.stderr)

    # 8. size
    total = 0
    for dp, dn, fn in os.walk(DIST):
        for f in fn:
            total += os.path.getsize(os.path.join(dp, f))
    print(f"== python_dist size: {total/1e6:.1f} MB ==")

if __name__ == "__main__":
    main()
