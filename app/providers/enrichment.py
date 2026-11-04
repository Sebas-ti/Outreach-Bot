"""Stage 2 - Contact enrichment (mobile phone).

SignalHire's Person API is asynchronous: we POST the LinkedIn URL (or email)
together with a callbackUrl, and SignalHire later POSTs the result to our
webhook (/webhooks/signalhire). The AwaitRegistry turns that into an awaitable
so the pipeline stays sequential and easy to reason about.
"""
import logging
from typing import Any, Protocol

import httpx

from ..models import Contact, EnrichmentResult
from ..utils import AwaitRegistry, normalize_phone

log = logging.getLogger(__name__)

PHONE_PRIORITY = {"mobile": 0, "personal": 1, "work_phone": 2, "work": 2, "fax": 9}


class Enricher(Protocol):
    async def enrich(self, contact: Contact) -> EnrichmentResult: ...


def linkedin_slug(url: str) -> str:
    """'https://co.linkedin.com/in/andr%C3%A9s-felipe/' -> 'andrés-felipe' (domain/encoding independent)."""
    from urllib.parse import unquote
    tail = (url or "").split("linkedin.com/in/", 1)[1]
    return unquote(tail.split("?")[0].split("#")[0].strip("/").split("/")[0]).lower()


def canonical_linkedin(url: str) -> str:
    from urllib.parse import quote
    return f"https://www.linkedin.com/in/{quote(linkedin_slug(url), safe='-_')}"


def enrichment_key(item: str) -> str:
    """Matching key for SignalHire callbacks: LinkedIn profiles compare by slug, emails case-insensitively."""
    item = (item or "").strip()
    if "linkedin.com/in/" in item:
        return "li:" + linkedin_slug(item)
    return item.lower().rstrip("/")


def parse_signalhire_item(entry: dict[str, Any], default_cc: str = "57") -> EnrichmentResult:
    """Parse one element of SignalHire's callback array."""
    status = entry.get("status")
    if status != "success":
        return EnrichmentResult(found=False, reason=f"signalhire: {status}", raw=entry)
    contacts = (entry.get("candidate") or {}).get("contacts") or []
    phones = [c for c in contacts if c.get("type") == "phone" and c.get("value")]
    emails = [c for c in contacts if c.get("type") == "email" and c.get("value")]
    phones.sort(key=lambda c: (PHONE_PRIORITY.get(c.get("subType", ""), 5), -int(c.get("rating") or 0)))
    phone = normalize_phone(phones[0]["value"], default_cc) if phones else ""
    email = emails[0]["value"] if emails else ""
    if not phone:
        return EnrichmentResult(found=False, email=email, reason="perfil encontrado sin teléfono", raw=entry)
    return EnrichmentResult(found=True, phone=phone, email=email, raw=entry)


class SignalHireEnricher:
    URL = "https://www.signalhire.com/api/v1/candidate/search"

    def __init__(self, api_key: str, callback_url: str, registry: AwaitRegistry,
                 timeout_s: int = 180, default_cc: str = "57"):
        if not api_key:
            raise ValueError("SIGNALHIRE_API_KEY no configurada")
        self.api_key = api_key
        self.callback_url = callback_url
        self.registry = registry
        self.timeout_s = timeout_s
        self.default_cc = default_cc

    async def enrich(self, contact: Contact) -> EnrichmentResult:
        item = canonical_linkedin(contact.linkedin_url) if contact.linkedin_url else contact.email
        if not item:
            return EnrichmentResult(found=False, reason="sin LinkedIn ni email para buscar")
        key = enrichment_key(item)
        self.registry.expect(f"sh:{key}")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                self.URL,
                headers={"apikey": self.api_key, "Content-Type": "application/json"},
                json={"items": [item], "callbackUrl": self.callback_url},
            )
        if r.status_code == 402:
            return EnrichmentResult(found=False, reason="SignalHire: sin créditos")
        if r.status_code >= 400:
            return EnrichmentResult(found=False, reason=f"SignalHire HTTP {r.status_code}: {r.text[:200]}")
        log.info("SignalHire request accepted: %s", r.text[:200])
        try:  # SignalHire echoes requestId in the callback's Request-Id header -> exact match
            self.registry.aliases[f"shreq:{r.json()['requestId']}"] = f"sh:{key}"
        except (ValueError, KeyError, TypeError):
            pass
        try:
            entry = await self.registry.wait(f"sh:{key}", self.timeout_s)
        except TimeoutError:
            return EnrichmentResult(found=False, reason=f"SignalHire no respondió en {self.timeout_s}s")
        return parse_signalhire_item(entry, self.default_cc)


class WaterfallEnricher:
    """Tries providers in order until one finds a phone (e.g. SignalHire -> Lusha -> ...)."""

    def __init__(self, providers: list[tuple[str, Enricher]]):
        self.providers = providers

    async def enrich(self, contact: Contact) -> EnrichmentResult:
        reasons, email = [], ""
        for name, p in self.providers:
            try:
                r = await p.enrich(contact)
            except Exception as e:  # one provider down must not stop the batch
                log.exception("enricher %s failed", name)
                r = EnrichmentResult(found=False, reason=f"error {type(e).__name__}")
            email = email or r.email
            if r.found:
                r.reason = f"{name} (tras {'; '.join(reasons)})" if reasons else name
                r.email = r.email or email
                return r
            reasons.append(f"{name}: {r.reason}")
        return EnrichmentResult(found=False, email=email, reason=" | ".join(reasons))


class MockEnricher:
    """Simulates the enrichment DB.

    'Finds' the consented test number, or a fictitious +57 300 555 01xx number for
    rows that define a demo scenario. Everything else -> no data (skip).
    """

    def __init__(self, default_cc: str = "57"):
        self.default_cc = default_cc

    async def enrich(self, contact: Contact) -> EnrichmentResult:
        if contact.consent_phone:
            return EnrichmentResult(found=True, phone=normalize_phone(contact.consent_phone, self.default_cc), reason="mock")
        if contact.demo_scenario and (contact.linkedin_url or contact.email):
            return EnrichmentResult(found=True, phone=f"+5730055501{contact.id % 100:02d}", reason="mock (ficticio)")
        return EnrichmentResult(found=False, reason="mock: sin datos de contacto")
