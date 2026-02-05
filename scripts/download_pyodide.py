import os
import sys
import tarfile
import urllib.request

URL = "https://github.com/pyodide/pyodide/releases/download/0.24.1/pyodide-0.24.1.tar.bz2"
DEST_DIR = os.path.join("static", "pyodide")
ARCHIVE = os.path.join(DEST_DIR, "pyodide-0.24.1.tar.bz2")
SIZE_FILE = os.path.join(DEST_DIR, "pyodide-0.24.1.size")


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

    extract_dir = os.path.join(DEST_DIR, "pyodide")
    if not os.path.exists(extract_dir):
        print("Extracting...")
        with tarfile.open(ARCHIVE, "r:bz2") as tf:
            tf.extractall(DEST_DIR)
        print("Extracted to", DEST_DIR)
    else:
        print("Already extracted.")

    # Move extracted content one level up if needed
    for name in os.listdir(DEST_DIR):
        if name.startswith("pyodide-") and os.path.isdir(os.path.join(DEST_DIR, name)):
            src = os.path.join(DEST_DIR, name)
            for item in os.listdir(src):
                os.replace(os.path.join(src, item), os.path.join(DEST_DIR, item))
            try:
                os.rmdir(src)
            except OSError:
                pass
            break

    print("Done. Pyodide should be in", DEST_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
