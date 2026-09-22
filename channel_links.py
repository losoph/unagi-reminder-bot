"""Recognize a public Telegram channel reference in free text."""
import re

# Telegram usernames: 5–32 chars, start with a letter.
_NAME = r"[A-Za-z][A-Za-z0-9_]{3,31}"
_HOSTS = r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)"
_LINK_RE = re.compile(rf"{_HOSTS}/(?:s/)?(?P<name>{_NAME})(?![A-Za-z0-9_])", re.IGNORECASE)
_RESOLVE_RE = re.compile(rf"tg://resolve\?domain=(?P<name>{_NAME})(?![A-Za-z0-9_])", re.IGNORECASE)
_MENTION_RE = re.compile(rf"^@(?P<name>{_NAME})$")
_BARE_RE = re.compile(rf"^(?P<name>{_NAME})$")
_PRIVATE_INVITE_RE = re.compile(rf"{_HOSTS}/(?:\+|joinchat/)", re.IGNORECASE)

# t.me paths that look like usernames but are service pages.
_RESERVED = {
    "joinchat", "addstickers", "addemoji", "addtheme", "addlist", "share", "proxy", "socks",
    "setlanguage", "login", "confirmphone", "invoice", "boost", "contact", "giftcode",
}


def _valid(name: str | None) -> str | None:
    if not name or name.lower() in _RESERVED or name.endswith("_") or "__" in name:
        return None
    return name


def find_channel_username(text: str | None, *, extra_urls: list[str] | None = None) -> str | None:
    """Return the channel username referenced by the message, without '@'.

    Links (t.me, t.me/s web view, post links, telegram.me, tg://resolve) are found
    anywhere in the text; a bare «@name» or «name» only counts as the whole message.
    """
    text = (text or "").strip()
    for candidate in [*(extra_urls or []), text]:
        for pattern in (_LINK_RE, _RESOLVE_RE):
            for match in pattern.finditer(candidate):
                if name := _valid(match.group("name")):
                    return name
    for pattern in (_MENTION_RE, _BARE_RE):
        if match := pattern.match(text):
            return _valid(match.group("name"))
    return None


def is_private_invite(text: str | None) -> bool:
    return bool(_PRIVATE_INVITE_RE.search(text or ""))
