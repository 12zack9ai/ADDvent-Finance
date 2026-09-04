"""Application configuration.

Everything is environment-driven so the same code runs on a laptop and on the
production server with no edits. Copy .env.example to .env and fill it in.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # --- storage -----------------------------------------------------------
    data_dir: Path = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    db_url: str = os.getenv("DATABASE_URL", "")

    # --- Claude ------------------------------------------------------------
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    extraction_model: str = os.getenv("EXTRACTION_MODEL", "claude-opus-5")

    # --- site --------------------------------------------------------------
    site_name: str = os.getenv("SITE_NAME", "Adventures Inc · Finance")
    base_url: str = os.getenv("BASE_URL", "http://127.0.0.1:8000")

    # --- access control ----------------------------------------------------
    # A single shared password gates the whole site. Deliberately simple for a
    # small internal team; see README for the path to per-user accounts / SSO.
    # If APP_PASSWORD is blank the site is OPEN - only acceptable on localhost.
    app_password: str = os.getenv("APP_PASSWORD", "").strip()
    secret_key: str = os.getenv("SECRET_KEY", "").strip()
    session_days: int = int(os.getenv("SESSION_DAYS", "14"))

    # --- PDF rendering -----------------------------------------------------
    # Path to a Chromium/Chrome binary used to render the marked-up PDF.
    chrome_binary: str = os.getenv("CHROME_BINARY", "")

    # --- Mailbox -----------------------------------------------------------
    # Two backends. IMAP is the simple one: an ordinary mailbox on your own mail
    # host (cPanel/Plesk), no app registration and nobody's approval needed.
    # Microsoft Graph is the alternative for Exchange / Microsoft 365.
    # Whichever is configured is the one that runs; IMAP wins if both are.
    mail_backend: str = os.getenv("MAIL_BACKEND", "auto").strip().lower()

    imap_host: str = os.getenv("IMAP_HOST", "").strip()
    imap_port: int = int(os.getenv("IMAP_PORT", "993"))
    imap_ssl: bool = _bool("IMAP_SSL", True)
    imap_user: str = os.getenv("IMAP_USER", "").strip()
    imap_password: str = os.getenv("IMAP_PASSWORD", "")
    imap_folder: str = os.getenv("IMAP_FOLDER", "INBOX").strip()

    # --- Microsoft Graph (Exchange / M365 mailbox) -------------------------
    # Off until IT provides the app registration. The app is fully usable
    # via upload while this is disabled.
    mail_enabled: bool = _bool("MAIL_ENABLED", False)
    ms_tenant_id: str = os.getenv("MS_TENANT_ID", "").strip()
    ms_client_id: str = os.getenv("MS_CLIENT_ID", "").strip()
    ms_client_secret: str = os.getenv("MS_CLIENT_SECRET", "").strip()
    ms_mailbox: str = os.getenv("MS_MAILBOX", "").strip()
    mail_poll_seconds: int = int(os.getenv("MAIL_POLL_SECONDS", "300"))
    mail_processed_folder: str = os.getenv("MAIL_PROCESSED_FOLDER", "Processed")

    # --- approval policy ---------------------------------------------------
    # Three-way match thresholds. A variance is "within tolerance" if it is
    # under BOTH the percentage and the flat amount is not exceeded - i.e. the
    # allowance is whichever of the two is greater, per the approval policy.
    tolerance_pct: float = float(os.getenv("TOLERANCE_PCT", "5"))
    tolerance_abs: str = os.getenv("TOLERANCE_ABS", "250")
    # Even a clean invoice above this total gets an owner spot-check.
    owner_review_above: str = os.getenv("OWNER_REVIEW_ABOVE", "5000")
    # Require confirmation of delivery / work completion before approval.
    require_receipt: bool = _bool("REQUIRE_RECEIPT", True)

    # --- matching ----------------------------------------------------------
    # Description similarity (0-100) required to consider two lines the same
    # item when SKUs are absent or differ.
    fuzzy_threshold: int = int(os.getenv("FUZZY_THRESHOLD", "88"))

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    def resolved_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        return f"sqlite:///{self.data_dir / 'finance.db'}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.renders_dir):
            d.mkdir(parents=True, exist_ok=True)

    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def graph_configured(self) -> bool:
        return bool(
            self.ms_tenant_id
            and self.ms_client_id
            and self.ms_client_secret
            and self.ms_mailbox
        )

    def active_mail_backend(self) -> str:
        """Which backend to use: "imap", "graph", or "" when none is set up."""
        if not self.mail_enabled:
            return ""
        if self.mail_backend == "imap":
            return "imap" if self.imap_configured() else ""
        if self.mail_backend == "graph":
            return "graph" if self.graph_configured() else ""
        # auto: prefer whichever is actually configured, IMAP first.
        if self.imap_configured():
            return "imap"
        if self.graph_configured():
            return "graph"
        return ""

    def mail_configured(self) -> bool:
        return bool(self.active_mail_backend())


settings = Settings()
settings.ensure_dirs()
