import asyncio
import re
import unicodedata
from typing import Any


def normalize_text(s: str) -> str:
    """lowercase, strip accents and punctuation -> 'jose perez'"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9 ]+", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def decode_csv(raw: bytes) -> str:
    """CSV files saved by Excel/Notepad on Windows are often cp1252, not UTF-8."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def normalize_url(url: str) -> str:
    """Percent-encode non-ASCII chars (linkedin.com/in/juan-sebastián -> juan-sebasti%C3%A1n)."""
    from urllib.parse import quote, urlsplit, urlunsplit
    if not url:
        return ""
    parts = urlsplit(url.strip())
    return urlunsplit(parts._replace(path=quote(parts.path, safe="/%-_.~")))


def normalize_phone(raw: str, default_cc: str = "57") -> str:
    """Return E.164 (+573001234567) or '' if it does not look like a phone."""
    if not raw:
        return ""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        result = "+" + digits
    elif raw.startswith("00"):
        result = "+" + digits[2:]
    elif len(digits) == 10 and default_cc == "57" and digits.startswith("3"):
        result = "+57" + digits  # Colombian mobile without country code
    elif digits.startswith(default_cc) and len(digits) > 10:
        result = "+" + digits
    else:
        result = "+" + default_cc + digits
    return result if 8 <= len(result) - 1 <= 15 else ""


class AwaitRegistry:
    """Bridges async webhooks back to the sequential pipeline.

    The pipeline awaits `wait(key)`; the webhook handler calls `resolve(key, value)`.
    Results that arrive before anyone waits are buffered.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future] = {}
        self._early: dict[str, Any] = {}
        self.aliases: dict[str, str] = {}  # e.g. "shreq:1748" -> "sh:li:juan-perez"

    def is_pending(self, key: str) -> bool:
        return key in self._futures and not self._futures[key].done()

    def pending(self, prefix: str) -> list[str]:
        return [k for k, f in self._futures.items() if k.startswith(prefix) and not f.done()]

    def expect(self, key: str) -> None:
        if key not in self._futures:
            self._futures[key] = asyncio.get_running_loop().create_future()
        if key in self._early:
            self._futures[key].set_result(self._early.pop(key))

    async def wait(self, key: str, timeout: float) -> Any:
        self.expect(key)
        try:
            return await asyncio.wait_for(self._futures[key], timeout)
        finally:
            self._futures.pop(key, None)

    def resolve(self, key: str, value: Any) -> bool:
        fut = self._futures.get(key)
        if fut and not fut.done():
            fut.set_result(value)
            return True
        self._early[key] = value
        return False
