"""Email copies of digests, sent through the same SMTP account as the server alerts.

SMTP settings come from SMTP_* environment variables; optionally an msmtp config
(the one the alert scripts use) mounted at MSMTPRC_PATH supplies the defaults.
The HTML body is rendered from the same node tree as the Telegraph page.
"""
import asyncio
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape

from telegraph_publisher import build_content

logger = logging.getLogger(__name__)

MSMTPRC_PATH = os.getenv("MSMTPRC_PATH", "/run/secrets/msmtprc")
SMTP_TIMEOUT = 30
SENDER_NAME = "Unagi"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str  # "ssl" | "starttls" | "none"
    user: str
    password: str
    sender: str


def _read_msmtprc(path: str) -> dict[str, str]:
    """Merge `defaults` with the default account (or the first one) of an msmtp config."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}
    sections: dict[str, dict[str, str]] = {"defaults": {}}
    order: list[str] = []
    default_account = None
    current = "defaults"
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(" ")
        value = value.strip()
        if key == "defaults":
            current = "defaults"
        elif key == "account":
            if value.startswith("default") and ":" in value:
                default_account = value.split(":", 1)[1].strip()
                continue
            current = value
            sections.setdefault(current, {})
            order.append(current)
        else:
            sections[current][key] = value
    account = default_account or (order[0] if order else "defaults")
    merged = dict(sections["defaults"])
    merged.update(sections.get(account, {}))
    if "passwordeval" in merged and "password" not in merged:
        logger.warning("msmtp passwordeval is not supported; set SMTP_PASSWORD instead")
    return merged


def load_config() -> SmtpConfig | None:
    rc = _read_msmtprc(MSMTPRC_PATH)
    host = os.getenv("SMTP_HOST") or rc.get("host", "")
    env_port = os.getenv("SMTP_PORT") or rc.get("port")
    if "host" in rc:
        tls = rc.get("tls", "off") == "on"
        starttls = rc.get("tls_starttls", "on") == "on"
        derived = "none" if not tls else "starttls" if starttls else "ssl"
    else:  # never fall back to a plaintext login when configured only through env
        derived = "ssl" if env_port == "465" else "starttls"
    security = (os.getenv("SMTP_SECURITY") or derived).lower()
    if security not in {"ssl", "starttls", "none"}:
        logger.warning("Unknown SMTP_SECURITY=%r, using starttls", security)
        security = "starttls"
    default_port = {"ssl": 465, "starttls": 587, "none": 25}[security]
    port = int(env_port or default_port)
    user = os.getenv("SMTP_USER") or rc.get("user", "")
    password = os.getenv("SMTP_PASSWORD") or rc.get("password", "")
    sender = os.getenv("SMTP_FROM") or rc.get("from", "") or user
    if not host or not EMAIL_RE.match(sender):
        return None
    return SmtpConfig(host, port, security, user, password, sender)


_CONFIG = load_config()


def is_enabled() -> bool:
    return _CONFIG is not None


def normalize_email(text: str | None) -> str | None:
    value = (text or "").strip()
    if len(value) > 254 or not EMAIL_RE.match(value):
        return None
    local, _, domain = value.partition("@")
    return f"{local}@{domain.lower()}"


# ---------------------------------------------------------------- rendering

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_TAG_STYLES = {
    "p": "margin:0 0 14px;line-height:1.55;",
    "h4": "margin:28px 0 10px;font-size:18px;line-height:1.3;color:#111827;",
    "ul": "margin:0 0 14px;padding-left:20px;line-height:1.55;",
    "li": "margin:0 0 4px;",
    "hr": "border:0;border-top:1px solid #e5e7eb;margin:22px 0;",
    "a": "color:#2481cc;text-decoration:none;font-weight:600;",
    "b": "",
}
_VOID_TAGS = {"hr", "br"}


def render_nodes(nodes: list) -> str:
    """Telegraph Node tree → email-safe HTML with inline styles (unknown tags keep only text)."""
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(escape(node, quote=False))
            continue
        tag = node.get("tag")
        children = render_nodes(node.get("children") or [])
        if tag not in _TAG_STYLES:
            parts.append(children)
            continue
        attrs = ""
        href = (node.get("attrs") or {}).get("href")
        if tag == "a":
            if not (isinstance(href, str) and href.startswith(("https://", "http://"))):
                parts.append(children)
                continue
            attrs += f' href="{escape(href, quote=True)}" target="_blank"'
        if _TAG_STYLES[tag]:
            attrs += f' style="{_TAG_STYLES[tag]}"'
        parts.append(f"<{tag}{attrs}>" if tag in _VOID_TAGS else f"<{tag}{attrs}>{children}</{tag}>")
    return "".join(parts)


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}" target="_blank" style="{_TAG_STYLES["a"]}">{escape(label)}</a>'


def build_digest_email(
    title: str,
    sections: list[dict],
    *,
    telegraph_url: str | None = None,
    manage_url: str | None = None,
) -> tuple[str, str]:
    """Return (html, plain_text) bodies for a digest."""
    body = render_nodes(build_content(sections))
    top = ""
    if telegraph_url:
        top = f'<p style="{_TAG_STYLES["p"]}">📖 {_link(telegraph_url, "Открыть в Telegraph")}</p>'
    footer = "Это копия дайджеста из Telegram-бота Unagi."
    if manage_url:
        footer += f" {_link(manage_url, 'Отключить письма')}"
    html_body = (
        "<!doctype html>"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f3f4f6;">'
        '<div style="padding:24px 12px;">'
        f'<div style="max-width:640px;margin:0 auto;background:#ffffff;border-radius:12px;padding:28px 24px;'
        f'font-family:{_FONT};font-size:16px;color:#1f2937;">'
        f'<h1 style="margin:0 0 16px;font-size:24px;line-height:1.25;color:#111827;">📰 {escape(title)}</h1>'
        f"{top}{body}"
        f'<p style="margin:24px 0 0;font-size:13px;line-height:1.5;color:#6b7280;">{footer}</p>'
        "</div></div></body></html>"
    )

    lines = [title, ""]
    if telegraph_url:
        lines += [f"Открыть в Telegraph: {telegraph_url}", ""]
    for section in sections:
        lines.append(f"== {section.get('title') or 'Канал'} ==")
        for post in section.get("posts", []):
            lines.append(f"- {(post.get('text') or '').strip()}")
            if post.get("link"):
                lines.append(f"  {post['link']}")
        lines.append("")
    if manage_url:
        lines.append(f"Отключить письма: {manage_url}")
    return html_body, "\n".join(lines)


# ---------------------------------------------------------------- sending


def _send_sync(config: SmtpConfig, message: EmailMessage) -> None:
    context = ssl.create_default_context()
    if config.security == "ssl":
        server = smtplib.SMTP_SSL(config.host, config.port, timeout=SMTP_TIMEOUT, context=context)
    else:
        server = smtplib.SMTP(config.host, config.port, timeout=SMTP_TIMEOUT)
    with server:
        if config.security == "starttls":
            server.starttls(context=context)
        if config.user:
            server.login(config.user, config.password)
        server.send_message(message)


async def send_email(
    to: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    *,
    unsubscribe_url: str | None = None,
) -> bool:
    if _CONFIG is None:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((SENDER_NAME, _CONFIG.sender))
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=_CONFIG.sender.rsplit("@", 1)[-1])
    if unsubscribe_url:
        message["List-Unsubscribe"] = f"<{unsubscribe_url}>"
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    try:
        await asyncio.to_thread(_send_sync, _CONFIG, message)
        return True
    except Exception:
        logger.exception("Email to %s failed", to.split("@", 1)[-1])
        return False
