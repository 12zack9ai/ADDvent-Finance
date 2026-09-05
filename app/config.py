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
    # Only needed when the API key is an ORGANISATION key rather than one scoped
    # to a workspace. A workspace-scoped key carries this implicitly; an org key
    # is rejected with "must include the anthropic-workspace-id header".
    anthropic_workspace_id: str = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()

    # --- site --------------------------------------------------------------
    site_name: str = os.getenv("SITE_NAME", "Add Ventures Inc · Finance")
    base_url: str = os.getenv("BASE_URL", "http://127.0.0.1:8000")

    # --- access control ----------------------------------------------------
    # A single shared password gates the whole site. Deliberately simple for a
    # small internal team; see README for the path to per-user accounts / SSO.
    # If APP_PASSWORD is blank the site is OPEN - only acceptable on localhost.
    app_password: str = os.getenv("APP_PASSWORD", "").strip()
    secret_key: str = os.getenv("SECRET_KEY", "").strip()
    session_days: int = int(os.getenv("SESSION_DAYS", "14"))

    # --- sending mail ------------------------------------------------------
    # Used to ask the sender which job a document belongs to when nothing on the
    # document or in the email says. Defaults are derived from the IMAP settings,
    # because on ordinary mail hosting it is the same mailbox and password.
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_user: str = os.getenv("SMTP_USER", "").strip()
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "").strip()
    # Off by default: replying to people is the one thing this app does that
    # leaves the building, and it should never start happening by surprise.
    ask_for_job_number: bool = _bool("ASK_FOR_JOB_NUMBER", False)

    # Who the app is allowed to email. Comma-separated domains; when unset it
    # falls back to the domain of our own mailbox, so the safe answer is the
    # default rather than something an administrator has to remember to set.
    # Vendors are deliberately NOT reachable yet - staff forward a vendor's
    # document in, and staff are who get asked which job it belongs to.
    reply_domains_raw: str = os.getenv("REPLY_DOMAINS", "").strip()

    # --- sample data -------------------------------------------------------
    # Loads samples/job-260000 into an empty install, once, in the background.
    # Off unless explicitly set; see scripts/load_samples.py.
    load_samples: bool = _bool("LOAD_SAMPLES", False)

    # Fills every programme - jobs, quotes, invoices, subcontracts, checks,
    # change orders, a flagged bill and a forecast - with the sample set in
    # scripts/seed_samples.py, so a fresh deploy shows what the app looks like
    # in use. Reserved to job numbers 269xxx and removable in one command:
    #   python scripts/seed_samples.py --remove
    seed_samples: bool = _bool("SEED_SAMPLES", False)

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

    # JobNimbus: read-only, and used for exactly one question - who is this job
    # assigned to, so we know who to ask for the missing quote.
    jobnimbus_api_key: str = os.getenv("JOBNIMBUS_API_KEY", "").strip()
    ask_for_quote: bool = _bool("ASK_FOR_QUOTE", False)
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
    # OFF. Requiring a receipt before every approval means a project manager
    # signs for every delivery on every job, which is a person's whole day and
    # is not how this business runs. Zack: "That means I'm gonna have to have a
    # project manager review that every single time. Leave that out for now."
    #
    # The machinery stays - the model, the routing, the confirm form - because
    # the three-way match is the right control if the receiving side ever gets
    # staffed. REQUIRE_RECEIPT=true turns it back on and nothing else changes.
    require_receipt: bool = _bool("REQUIRE_RECEIPT", False)

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

    def smtp_settings(self) -> tuple[str, int, str, str, str]:
        """Host, port, user, password, from-address for sending.

        Falls back to the IMAP credentials, because on ordinary mail hosting
        (cPanel, Plesk) it is the same mailbox with the same password, and
        asking someone to type it twice invites one of the two being wrong.
        """
        host = self.smtp_host or self.imap_host
        user = self.smtp_user or self.imap_user
        password = self.smtp_password or self.imap_password
        sender = self.smtp_from or user
        return host, self.smtp_port, user, password, sender

    def reply_domains(self) -> set[str]:
        """Domains this app may send to. Empty means send to nobody."""
        listed = {
            d.strip().lower().lstrip("@")
            for d in self.reply_domains_raw.split(",")
            if d.strip()
        }
        if listed:
            return listed
        # Our own mailbox's domain. Replying to ourselves is the conservative
        # default; anything wider has to be asked for explicitly.
        _, _, _, _, sender = self.smtp_settings()
        domain = sender.rsplit("@", 1)[-1].strip().lower() if "@" in sender else ""
        return {domain} if domain else set()

    def may_email(self, address: str) -> bool:
        domain = (address or "").rsplit("@", 1)[-1].strip().lower()
        return bool(domain) and domain in self.reply_domains()

    def can_send_mail(self) -> bool:
        host, _, user, password, sender = self.smtp_settings()
        return bool(host and user and password and sender)

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
