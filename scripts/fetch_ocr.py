"""Install the OCR engine the page uses to read scanned PDFs: Tesseract compiled for the browser (tesseract.js).

    python scripts/fetch_ocr.py

Downloads pinned packages from the npm registry (needs Node's npm) and copies the five files the page needs into
apps/review/vendor/tesseract/, which this server then serves itself: scanned pages are read in the browser and never
sent anywhere. About 11 MB; the folder is git-ignored. Licences: tesseract.js and tesseract.js-core Apache-2.0,
the English model (@tesseract.js-data/eng) MIT, Tesseract's own data Apache-2.0.
"""
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "apps" / "review" / "vendor" / "tesseract"
PACKAGES = ["tesseract.js@5.1.1", "tesseract.js-core@5.1.1", "@tesseract.js-data/eng@1.0.0"]
FILES = {   # (package folder, path inside the package) -> where the page looks for it
    ("tesseract.js", "dist/tesseract.min.js"): "tesseract.min.js",
    ("tesseract.js", "dist/worker.min.js"): "worker.min.js",
    ("tesseract.js-core", "tesseract-core-lstm.wasm.js"): "core/tesseract-core-lstm.wasm.js",
    ("tesseract.js-core", "tesseract-core-simd-lstm.wasm.js"): "core/tesseract-core-simd-lstm.wasm.js",
    ("eng", "4.0.0_best_int/eng.traineddata.gz"): "lang/eng.traineddata.gz",
}


def main() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        sys.exit("npm (Node.js) is needed to download the OCR engine.")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        subprocess.run([npm, "pack", *PACKAGES, "--silent"], cwd=work, check=True, capture_output=True)
        unpacked = {}
        for archive in work.glob("*.tgz"):
            name = "eng" if "data-eng" in archive.name else archive.name.rsplit("-", 1)[0]
            folder = work / name
            with tarfile.open(archive) as tar:
                tar.extractall(folder, filter="data")
            unpacked[name] = folder / "package"
        if TARGET.exists():
            shutil.rmtree(TARGET)
        for (package, inner), dest in FILES.items():
            source = unpacked[package] / inner
            if not source.is_file():
                sys.exit(f"{package} does not contain {inner}; the pinned version may have changed.")
            out = TARGET / dest
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, out)
            print(f"  {dest} ({out.stat().st_size // 1024} KB)")
    (TARGET / "LICENSES.txt").write_text(
        "tesseract.js 5.1.1 and tesseract.js-core 5.1.1: Apache-2.0 (https://github.com/naptha/tesseract.js)\n"
        "@tesseract.js-data/eng 1.0.0: MIT; Tesseract trained data: Apache-2.0 (https://github.com/tesseract-ocr)\n",
        encoding="utf-8")
    print(f"OCR engine installed in {TARGET}")


if __name__ == "__main__":
    main()
