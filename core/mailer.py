"""Application mailer — the ONLY path in AgentOS that sends email (Principle 3, §0).

Sends a job application email (cover + optional CV attachment) to a posting's apply
address. This is deliberately separate from the email-reply path (which stays draft-only):
send() here is invoked exclusively AFTER Calvin approves an application. There is no flag
that relaxes that -- the AUTO_APPLY bypass was removed, because a config switch reaching
"never ask before sending in his name" is the same hole Phase 30 closed for learned
permissions. Interview/form answers never route through here.
"""

from __future__ import annotations

import base64
import mimetypes
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from core.config import get_settings
from core.logging_setup import get_logger

log = get_logger("core.mailer")


class ApplicationMailer:
    """Sends approved application emails. Inject a Gmail service for testing."""

    def __init__(self, service: Any | None = None, user_id: str = "me") -> None:
        self._service = service
        self.user_id = user_id

    @property
    def service(self) -> Any:
        if self._service is None:
            from core.gmail_client import build_service

            self._service = build_service()
        return self._service

    def send_email(
        self, *, to: str, subject: str, body: str, attachments: list[str] | None = None
    ) -> dict[str, Any]:
        """Send a plain email. Same transport as an application; the CALLER owns the gate.

        Kept distinct from send_application only so intent is legible in the logs. Every caller
        must have an explicit confirmation first -- email_agent.compose requires a second
        'confirm send' step before this is ever reached (§0 P3).
        """
        return self.send_application(to=to, subject=subject, body=body, attachments=attachments)

    def send_application(
        self, *, to: str, subject: str, body: str, attachments: list[str] | None = None
    ) -> dict[str, Any]:
        """Send an application email with optional file attachments. Returns the sent message.

        Caller MUST have obtained Calvin's explicit approval before invoking this.
        """
        mime = MIMEMultipart()
        mime["To"] = to
        mime["From"] = get_settings().my_email
        mime["Subject"] = subject
        mime.attach(MIMEText(body, "plain"))

        for path_str in attachments or []:
            path = Path(path_str)
            if not path.exists():
                log.warning("Attachment not found, skipping: %s", path)
                continue
            ctype, _ = mimetypes.guess_type(str(path))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            part = MIMEApplication(path.read_bytes(), _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=path.name)
            mime.attach(part)

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        sent = (
            self.service.users()
            .messages()
            .send(userId=self.user_id, body={"raw": raw})
            .execute()
        )
        log.info("Sent application to %s (message %s)", to, sent.get("id"))
        return sent
