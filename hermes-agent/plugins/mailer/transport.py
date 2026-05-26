"""Transport implementations: dry-run, Resend, SMTP.

The dry-run transport is always selected when EMAIL_DRY_RUN=true regardless
of EMAIL_TRANSPORT — belt-and-suspenders so a misconfigured `.env` can't
accidentally send live during dev.

Resend SDK and smtplib have different failure modes; we normalize them into
either `PreSendError` (transport rejected before bytes left the host —
doesn't burn the per-recipient daily quota) or a regular Exception (which
the handler treats as post-send-unknown — DOES burn the quota, conservative).
"""

from __future__ import annotations

import base64
import smtplib
import socket
import ssl
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Protocol

from .attachments import Attachment
from .errors import PreSendError


@dataclass(frozen=True)
class RenderedEmail:
    to: str
    from_: str
    reply_to: str | None
    subject: str
    text: str
    html: str | None
    attachments: list[Attachment]


class Transport(Protocol):
    name: str

    def send(self, msg: RenderedEmail) -> str: ...


class DryRunTransport:
    name = "dry_run"

    def __init__(self, dryrun_dir: Path):
        self.dryrun_dir = dryrun_dir

    def send(self, msg: RenderedEmail) -> str:
        self.dryrun_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        msg_id = f"dryrun-{uuid.uuid4().hex}"
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_to = msg.to.replace("/", "_").replace("..", "_")
        out_path = self.dryrun_dir / f"{ts}-{safe_to}-{msg_id}.eml"
        em = _build_email_message(msg, message_id=f"<{msg_id}@dryrun.local>")
        out_path.write_bytes(bytes(em))
        try:
            out_path.chmod(0o600)
        except OSError:
            pass
        return msg_id


class ResendTransport:
    name = "resend"

    def __init__(self, api_key: str):
        if not api_key:
            raise PreSendError("resend_api_key_missing")
        self.api_key = api_key

    def send(self, msg: RenderedEmail) -> str:
        try:
            import resend  # type: ignore
        except ImportError as e:
            raise PreSendError(f"resend_module_not_installed: {e}") from e

        resend.api_key = self.api_key
        params: dict = {
            "from": msg.from_,
            "to": [msg.to],
            "subject": msg.subject,
            "text": msg.text,
        }
        if msg.html:
            params["html"] = msg.html
        if msg.reply_to:
            params["reply_to"] = msg.reply_to
        if msg.attachments:
            params["attachments"] = [
                {
                    "filename": a.name,
                    "content": base64.b64encode(a.content).decode("ascii"),
                    "content_type": a.mime,
                }
                for a in msg.attachments
            ]
        try:
            resp = resend.Emails.send(params)
        except Exception as e:
            # The SDK raises different classes across versions; map known
            # client-side rejections to PreSendError, treat everything else
            # as post-send-unknown by re-raising.
            cls_name = type(e).__name__
            if cls_name in {
                "ResendValidationError",
                "ResendBadRequestError",
                "ResendNotFoundError",
                "ResendUnauthorizedError",
                "ResendForbiddenError",
            }:
                raise PreSendError(f"resend_rejected: {e}") from e
            # Some SDK versions stash the HTTP status on the exception.
            status = getattr(e, "code", None) or getattr(e, "status_code", None)
            if isinstance(status, int) and 400 <= status < 500:
                raise PreSendError(f"resend_rejected_{status}: {e}") from e
            raise

        # Normalize the response shape — dict or object across SDK versions.
        msg_id = None
        if isinstance(resp, dict):
            msg_id = resp.get("id")
        else:
            msg_id = getattr(resp, "id", None)
        return msg_id or f"resend-noid-{uuid.uuid4().hex}"


class SMTPTransport:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        starttls: bool,
    ):
        if not host:
            raise PreSendError("smtp_host_missing")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.starttls = starttls

    def send(self, msg: RenderedEmail) -> str:
        em = _build_email_message(msg, message_id=make_msgid(domain="hermes.local"))
        ctx = ssl.create_default_context()
        # TLS mode is decided by the port, not the flag (cleaner contract):
        #   - port 465 → implicit TLS via SMTP_SSL. starttls flag ignored.
        #   - port != 465 → plain SMTP + optional STARTTLS upgrade.
        # The SMTP_STARTTLS flag only controls whether we attempt STARTTLS on
        # non-465 ports. We fail closed if STARTTLS is required but the
        # server doesn't support it.
        use_implicit_tls = self.port == 465
        try:
            if use_implicit_tls:
                smtp = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=30)
            else:
                smtp = smtplib.SMTP(self.host, self.port, timeout=30)
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            raise PreSendError(f"smtp_connect_failed: {e}") from e

        try:
            with smtp:
                if not use_implicit_tls and self.starttls:
                    try:
                        smtp.ehlo()
                        smtp.starttls(context=ctx)
                        smtp.ehlo()
                    except smtplib.SMTPException as e:
                        raise PreSendError(f"smtp_starttls_failed: {e}") from e
                elif not use_implicit_tls and not self.starttls:
                    # Plain SMTP with no encryption. Almost always wrong;
                    # we allow it for local relays / testing only.
                    smtp.ehlo()
                if self.username:
                    try:
                        smtp.login(self.username, self.password or "")
                    except smtplib.SMTPAuthenticationError as e:
                        raise PreSendError(f"smtp_auth_failed: {e}") from e
                    except smtplib.SMTPException as e:
                        # Other auth-time errors are also pre-send rejections.
                        raise PreSendError(f"smtp_auth_error: {e}") from e
                # The send is the post-send-unknown boundary. Anything that
                # raises here may have actually delivered the message.
                smtp.send_message(em)
        except PreSendError:
            raise
        except Exception:
            raise
        return em["Message-ID"]


def _build_email_message(msg: RenderedEmail, *, message_id: str) -> EmailMessage:
    em = EmailMessage()
    em["From"] = msg.from_
    em["To"] = msg.to
    em["Subject"] = msg.subject
    if msg.reply_to:
        em["Reply-To"] = msg.reply_to
    em["Message-ID"] = message_id
    em["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    em.set_content(msg.text)
    if msg.html:
        em.add_alternative(msg.html, subtype="html")
    for a in msg.attachments:
        maintype, _, subtype = a.mime.partition("/")
        em.add_attachment(
            a.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=a.name,
        )
    return em


def make_transport(*, dry_run: bool, transport_name: str, cfg) -> Transport:
    """Factory. `cfg` is the Config dataclass; we read only what we need."""
    if dry_run or transport_name == "dry_run":
        return DryRunTransport(cfg.dryrun_dir)
    if transport_name == "resend":
        return ResendTransport(api_key=cfg.resend_api_key or "")
    if transport_name == "smtp":
        return SMTPTransport(
            host=cfg.smtp_host or "",
            port=cfg.smtp_port,
            username=cfg.smtp_username,
            password=cfg.smtp_password,
            starttls=cfg.smtp_starttls,
        )
    raise PreSendError(f"unknown_transport: {transport_name!r}")
