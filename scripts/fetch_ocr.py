"""Install the OCR engine the page uses to read scanned PDFs: Tesseract compiled for the browser (tesseract.js).

    python scripts/fetch_ocr.py

Downloads three pinned packages from the npm registry (no npm needed), checks each against its published SHA-512,
and copies the five files the page needs into apps/review/vendor/tesseract/, which this server then serves itself:
scanned pages are read in the browser and never sent anywhere. About 11 MB; the folder is git-ignored. Licences:
tesseract.js and tesseract.js-core Apache-2.0, the English model (@tesseract.js-data/eng) MIT, Tesseract's own data
Apache-2.0.
"""
import base64
import hashlib
import io
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "apps" / "review" / "vendor" / "tesseract"
REGISTRY = "https://registry.npmjs.org"
PACKAGES = {   # name -> (tarball path, published sha512 integrity)
    "tesseract.js": ("tesseract.js/-/tesseract.js-5.1.1.tgz",
                     "sha512-lzVl/Ar3P3zhpUT31NjqeCo1f+D5+YfpZ5J62eo2S14QNVOmHBTtbchHm/YAbOOOzCegFnKf4B3Qih9LuldcYQ=="),
    "tesseract.js-core": ("tesseract.js-core/-/tesseract.js-core-5.1.1.tgz",
                          "sha512-KX3bYSU5iGcO1XJa+QGPbi+Zjo2qq6eBhNjSGR5E5q0JtzkoipJKOUQD7ph8kFyteCEfEQ0maWLu8MCXtvX5uQ=="),
    "eng": ("@tesseract.js-data/eng/-/eng-1.0.0.tgz",
            "sha512-mbTumm6KQPUHyzTPQaF3ObXYnx0SqqfV2nabqFVQBwD6Kl7PhGSLSzOlfFTWy0P3BjghaSKA2W9GB19Jk+ZcTg=="),
}
FILES = {   # (package, path inside the package) -> where the page looks for it
    ("tesseract.js", "dist/tesseract.min.js"): "tesseract.min.js",
    ("tesseract.js", "dist/worker.min.js"): "worker.min.js",
    ("tesseract.js-core", "tesseract-core-lstm.wasm.js"): "core/tesseract-core-lstm.wasm.js",
    ("tesseract.js-core", "tesseract-core-simd-lstm.wasm.js"): "core/tesseract-core-simd-lstm.wasm.js",
    ("eng", "4.0.0_best_int/eng.traineddata.gz"): "lang/eng.traineddata.gz",
}


def download(name: str) -> tarfile.TarFile:
    path, integrity = PACKAGES[name]
    with urllib.request.urlopen(f"{REGISTRY}/{path}", timeout=120) as res:
        data = res.read()
    digest = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != integrity:
        sys.exit(f"{name}: the download does not match its published SHA-512; refusing to install it.")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def main() -> None:
    archives = {name: download(name) for name in PACKAGES}
    if TARGET.exists():
        shutil.rmtree(TARGET)
    for (package, inner), dest in FILES.items():
        member = archives[package].extractfile(f"package/{inner}")
        if member is None:
            sys.exit(f"{package} does not contain {inner}; the pinned version may have changed.")
        out = TARGET / dest
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(member.read())
        print(f"  {dest} ({out.stat().st_size // 1024} KB)")
    (TARGET / "LICENSES.txt").write_text(
        "tesseract.js 5.1.1 and tesseract.js-core 5.1.1: Apache-2.0 (https://github.com/naptha/tesseract.js)\n"
        "@tesseract.js-data/eng 1.0.0: MIT; Tesseract trained data: Apache-2.0 (https://github.com/tesseract-ocr)\n",
        encoding="utf-8")
    print(f"OCR engine installed in {TARGET}")


if __name__ == "__main__":
    main()
