"""Magnet URI helpers — Python port of Magnetar's core/Magnets.kt.

Keeps the same conventions, including the '+' -> space handling in display
names and the tracker augmentation used to revive dead torrents.
"""
from __future__ import annotations

import base64
import binascii
import re
import urllib.parse

from .config import DEFAULT_TRACKERS

_XT_RE = re.compile(r"xt=urn:btih:([A-Za-z0-9]+)", re.IGNORECASE)
_TR_RE = re.compile(r"[?&]tr=([^&]+)", re.IGNORECASE)


def is_magnet(uri: str) -> bool:
    return bool(uri) and uri.strip().lower().startswith("magnet:?")


def info_hash(uri: str) -> str | None:
    """40-char lowercase hex info hash from a magnet URI (base32 also accepted)."""
    m = _XT_RE.search(uri)
    if not m:
        return None
    raw = m.group(1)
    if len(raw) == 40:
        try:
            int(raw, 16)
        except ValueError:
            return None
        return raw.lower()
    if len(raw) == 32:  # base32-encoded hash (old magnet style)
        try:
            return base32_to_hex(raw)
        except (binascii.Error, ValueError):
            return None
    return None


def base32_to_hex(raw: str) -> str:
    padded = raw.upper() + "=" * ((8 - len(raw) % 8) % 8)
    return binascii.hexlify(base64.b32decode(padded)).decode()


def display_name(uri: str) -> str | None:
    """The dn= parameter. '+' means space *before* percent-decoding (Magnetar rule)."""
    for part in uri.split("?")[-1].split("&"):
        if part.lower().startswith("dn="):
            value = part[3:].replace("+", " ")
            return urllib.parse.unquote(value)
    return None


def trackers(uri: str) -> list[str]:
    return [urllib.parse.unquote(t) for t in _TR_RE.findall(uri)]


def augment(uri: str, extra_trackers: list[str] | None = None) -> str:
    """Append the default trackers to a magnet, skipping ones already present.

    This is the 'revive a dead torrent' trick Magnetar borrowed from TorrDroid:
    more announce URLs = more chances to find peers from a Colab VM that can
    only make outbound connections.
    """
    uri = uri.strip()
    if not is_magnet(uri):
        return uri
    existing = {t.strip().lower() for t in trackers(uri)}
    additions = []
    for t in (extra_trackers if extra_trackers is not None else DEFAULT_TRACKERS):
        if t.strip().lower() not in existing:
            additions.append("tr=" + urllib.parse.quote(t, safe=""))
    if not additions:
        return uri
    return uri + ("&" if "?" in uri else "?") + "&".join(additions)
