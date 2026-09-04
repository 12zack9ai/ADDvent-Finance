"""Render HTML to PDF with headless Chromium.

Chromium is used rather than a Python PDF library because the marked-up invoice
is defined once as HTML/CSS and then served two ways - on screen and as a PDF -
from exactly the same template. One source, no drift between what a reviewer
sees in the browser and what gets printed or emailed to a vendor.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from app.config import settings

# Ordered by preference, covering the three places this actually runs: a Linux
# server, a developer's Mac, and Windows. Missing the macOS paths meant "Download
# PDF" failed on the one machine most likely to be used for a first trial.
_CANDIDATES = [
    # Linux
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

_cached: Optional[str] = None


class PdfUnavailable(RuntimeError):
    """Raised when no Chromium binary can be found."""


def find_chrome() -> Optional[str]:
    global _cached
    if _cached:
        return _cached

    if settings.chrome_binary and Path(settings.chrome_binary).exists():
        _cached = settings.chrome_binary
        return _cached

    for candidate in _CANDIDATES:
        if Path(candidate).exists():
            _cached = candidate
            return _cached

    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable", "chrome", "msedge"):
        found = shutil.which(name)
        if found:
            _cached = found
            return _cached

    # Glob the Playwright cache in case the version differs from the pin above.
    for base in (Path("/opt/pw-browsers"), Path.home() / ".cache/ms-playwright"):
        if base.exists():
            for match in sorted(base.glob("chromium-*/chrome-linux/chrome")):
                _cached = str(match)
                return _cached
    return None


def pdf_available() -> bool:
    return find_chrome() is not None


def render_html_to_pdf(html: str, out_path: Path) -> Path:
    """Render an HTML string to a PDF file. Returns the output path."""
    chrome = find_chrome()
    if not chrome:
        raise PdfUnavailable(
            "No Chromium binary found. Install it (apt-get install -y chromium) "
            "or set CHROME_BINARY in .env."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        source = tmp_dir / "page.html"
        source.write_text(html, encoding="utf-8")

        cmd = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--no-pdf-header-footer",
            f"--user-data-dir={tmp_dir / 'profile'}",
            f"--print-to-pdf={out_path}",
            source.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=120)

    if not out_path.exists() or out_path.stat().st_size == 0:
        detail = proc.stderr.decode("utf-8", "replace")[-800:]
        raise PdfUnavailable(f"Chromium failed to produce a PDF.\n{detail}")

    return out_path
