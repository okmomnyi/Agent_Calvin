"""Gmail API wrapper for AgentOS.

Wraps google-api-python-client with the OAuth desktop flow described in the build spec:
`python manage.py auth` on the laptop mints token.json, which is copied to the droplet.
Uses gmail.modify for reading, relabelling, archiving, drafts, and recoverable Trash.
Permanent deletion is deliberately not exposed. Trash actions are called only after the
email skill previews exact messages and receives explicit confirmation.
"""

from __future__ import annotations

import base64
import os
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.gmail")

# gmail.modify: read + label + draft + recoverable trash, but no permanent deletion (§0).
# gmail.send: used ONLY by core.mailer for approved job applications (Principle 3). The
# email-reply path (GmailClient) exposes no send method, so replies remain draft-only.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

LABEL_PREFIX = "AgentOS"


class GmailAuthError(RuntimeError):
    """Raised when credentials/token are missing or cannot be refreshed."""


def _token_path() -> Path:
    """Where token.json lives.

    Overridable because in Docker the project root is a read-only-ish image layer, while the
    token has to sit on a mounted volume: `_load_credentials` rewrites it whenever the access
    token is refreshed, so it must be somewhere writable that survives a rebuild.
    """
    override = os.getenv("GOOGLE_TOKEN_PATH", "").strip()
    return Path(override) if override else get_settings().project_root / "token.json"


def _credentials_path() -> Path:
    return get_settings().project_root / "credentials.json"


def run_oauth_flow() -> Path:
    """Run the InstalledAppFlow desktop OAuth flow and write token.json. Laptop-only.

    Returns the token path. Requires credentials.json (OAuth *desktop* client) in the
    project root — download it from Google Cloud Console.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_file = _credentials_path()
    if not creds_file.exists():
        raise GmailAuthError(
            f"Missing {creds_file}. Download an OAuth *desktop app* client secret from "
            "Google Cloud Console and save it there before running auth."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file = _token_path()
    token_file.write_text(creds.to_json(), encoding="utf-8")
    log.info("Wrote %s — copy it to the droplet's secrets/ directory.", token_file)
    return token_file


def _load_credentials() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = _token_path()
    if not token_file.exists():
        raise GmailAuthError(
            f"Missing {token_file}. Run `python manage.py auth` on the laptop first."
        )
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise GmailAuthError("Gmail token invalid and cannot be refreshed — re-run auth.")
    return creds


def build_service() -> Any:
    """Build an authenticated Gmail API service object (raises GmailAuthError if unauthed)."""
    from googleapiclient.discovery import build

    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


class GmailClient:
    """Clean method surface over the Gmail service. Inject a service for testing."""

    def __init__(self, service: Any | None = None, user_id: str = "me") -> None:
        self._service = service
        self.user_id = user_id
        self._label_cache: dict[str, str] = {}

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = build_service()
        return self._service

    # ------------------------------------------------------------- token status
    @staticmethod
    def token_status() -> dict[str, Any]:
        """Report whether token.json exists / is expired — used by /api/health."""
        token_file = _token_path()
        if not token_file.exists():
            return {"present": False, "expired": None}
        try:
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            return {"present": True, "expired": bool(creds.expired), "valid": bool(creds.valid)}
        except Exception as exc:  # noqa: BLE001
            return {"present": True, "error": str(exc)}

    # ------------------------------------------------------------- labels
    def _refresh_label_cache(self) -> None:
        resp = self.service.users().labels().list(userId=self.user_id).execute()
        self._label_cache = {lbl["name"]: lbl["id"] for lbl in resp.get("labels", [])}

    def get_or_create_label(self, name: str) -> str:
        """Return the label id for `name`, creating it (nested under AgentOS/) if absent."""
        if not self._label_cache:
            self._refresh_label_cache()
        if name in self._label_cache:
            return self._label_cache[name]
        created = (
            self.service.users()
            .labels()
            .create(
                userId=self.user_id,
                body={"name": name, "labelListVisibility": "labelShow",
                      "messageListVisibility": "show"},
            )
            .execute()
        )
        self._label_cache[name] = created["id"]
        log.info("Created Gmail label '%s'", name)
        return created["id"]

    def category_label(self, category: str) -> str:
        """Return the id of AgentOS/<Category> (title-cased)."""
        return self.get_or_create_label(f"{LABEL_PREFIX}/{category.replace('_', ' ').title()}")

    # ------------------------------------------------------------- messages
    def list_inbox(self, max_results: int = 50, query: str = "in:inbox") -> list[str]:
        """Return message ids matching a query (default: current inbox)."""
        resp = (
            self.service.users()
            .messages()
            .list(userId=self.user_id, q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]

    def get_message(self, msg_id: str, fmt: str = "metadata") -> dict[str, Any]:
        return (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=msg_id, format=fmt)
            .execute()
        )

    @staticmethod
    def header(message: dict[str, Any], name: str) -> str:
        for h in message.get("payload", {}).get("headers", []):
            if h.get("name", "").lower() == name.lower():
                return h.get("value", "")
        return ""

    def add_label(self, msg_id: str, label_id: str) -> None:
        self.service.users().messages().modify(
            userId=self.user_id, id=msg_id, body={"addLabelIds": [label_id]}
        ).execute()

    def archive(self, msg_id: str, category_label_id: str | None = None) -> None:
        """Archive: remove INBOX and apply a category label. NEVER trashes (§0)."""
        body: dict[str, Any] = {"removeLabelIds": ["INBOX"]}
        if category_label_id:
            body["addLabelIds"] = [category_label_id]
        self.service.users().messages().modify(
            userId=self.user_id, id=msg_id, body=body
        ).execute()

    def trash(self, msg_id: str) -> None:
        """Move one explicitly confirmed message to Gmail Trash (recoverable, never permanent)."""
        self.service.users().messages().trash(userId=self.user_id, id=msg_id).execute()

    def untrash(self, msg_id: str) -> None:
        """Restore a message previously moved to Trash."""
        self.service.users().messages().untrash(userId=self.user_id, id=msg_id).execute()

    # ------------------------------------------------------------- drafts (never send)
    def create_draft(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """Create a Gmail DRAFT. This class intentionally exposes NO send method (§0)."""
        mime = MIMEText(body)
        mime["To"] = to
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        draft = (
            self.service.users()
            .drafts()
            .create(userId=self.user_id, body={"message": message})
            .execute()
        )
        log.info("Created Gmail draft %s (to %s)", draft.get("id"), to)
        return draft
