import os
import shutil
import sys
import tarfile
import urllib.request

URL = "https://github.com/pyodide/pyodide/releases/download/0.24.1/pyodide-0.24.1.tar.bz2"
DEST_DIR = os.path.join("static", "pyodide")
ARCHIVE = os.path.join(DEST_DIR, "pyodide-0.24.1.tar.bz2")
SIZE_FILE = os.path.join(DEST_DIR, "pyodide-0.24.1.size")
REQUIRED_FILES = ("pyodide.js", "pyodide.asm.wasm", "python_stdlib.zip")


def _has_required_runtime(path):
    return all(os.path.exists(os.path.join(path, name)) for name in REQUIRED_FILES)


def _resolve_runtime_dir():
    direct = DEST_DIR
    nested = os.path.join(DEST_DIR, "pyodide")
    if _has_required_runtime(direct):
        return direct
    if _has_required_runtime(nested):
        return nested
    return None


def get_total_size():
    if os.path.exists(SIZE_FILE):
        with open(SIZE_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    req = urllib.request.Request(URL, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        size = int(resp.headers.get("Content-Length", "0"))
    with open(SIZE_FILE, "w", encoding="utf-8") as f:
        f.write(str(size))
    return size


def download_chunk(start, end):
    req = urllib.request.Request(URL)
    req.add_header("Range", f"bytes={start}-{end}")
    with urllib.request.urlopen(req) as resp, open(ARCHIVE, "ab") as f:
        f.write(resp.read())


def main():
    os.makedirs(DEST_DIR, exist_ok=True)
    total = get_total_size()
    current = os.path.getsize(ARCHIVE) if os.path.exists(ARCHIVE) else 0
    if current >= total:
        print("Archive already downloaded.")
    else:
        chunk_size = 8 * 1024 * 1024
        while current < total:
            end = min(current + chunk_size - 1, total - 1)
            download_chunk(current, end)
            current = os.path.getsize(ARCHIVE)
            print(f"Downloaded {current}/{total}")

    runtime_dir = _resolve_runtime_dir()
    if not runtime_dir:
        print("Extracting...")
        nested_dir = os.path.join(DEST_DIR, "pyodide")
        if os.path.isdir(nested_dir):
            shutil.rmtree(nested_dir, ignore_errors=True)

        with tarfile.open(ARCHIVE, "r:bz2") as tf:
            tf.extractall(DEST_DIR)

        # Move extracted content one level up if needed
        nested_runtime = os.path.join(DEST_DIR, "pyodide")
        if os.path.isdir(nested_runtime):
            for item in os.listdir(nested_runtime):
                src = os.path.join(nested_runtime, item)
                dst = os.path.join(DEST_DIR, item)
                if os.path.exists(dst):
                    if os.path.isdir(dst):
                        shutil.rmtree(dst, ignore_errors=True)
                    else:
                        os.remove(dst)
                os.replace(src, dst)
            shutil.rmtree(nested_runtime, ignore_errors=True)

        print("Extracted to", DEST_DIR)
    else:
        print("Already extracted.")

    if not _has_required_runtime(DEST_DIR):
        print("[ERROR] Pyodide extracted incorrectly. Required runtime files are missing.")
        return 1

    print("Done. Pyodide should be in", DEST_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
