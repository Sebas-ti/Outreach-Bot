"""Stage 1 - LinkedIn profile identification.

We do NOT scrape LinkedIn (forbidden by its User Agreement). Instead we query a
search engine API (SerpAPI -> Google) with `site:linkedin.com/in "Name" "Company"`
and score each public result against the contact data.
"""
import logging
from typing import Protocol

import httpx

from ..models import Contact, LinkedInMatch
from ..utils import normalize_text

log = logging.getLogger(__name__)


class LinkedInFinder(Protocol):
    async def find(self, contact: Contact) -> LinkedInMatch | None: ...


def score_result(contact: Contact, title: str, snippet: str, link: str) -> float:
    """0..1 confidence that a search result is this contact's profile.

    name match 0.6 (title of the result) + company 0.3 + job title 0.1
    """
    if "linkedin.com/in/" not in (link or ""):
        return 0.0
    head = normalize_text(title)
    body = normalize_text(f"{title} {snippet}")

    name_tokens = normalize_text(contact.full_name).split()
    if not name_tokens:
        return 0.0
    name_score = sum(t in head.split() for t in name_tokens) / len(name_tokens)
    # first name AND at least one surname must be present
    if normalize_text(contact.first_name).split()[0] not in head.split() or name_score < 0.5:
        return round(name_score * 0.3, 2)

    company_score = 0.0
    company = normalize_text(contact.company)
    if company:
        if company in body:
            company_score = 1.0
        else:
            words = [w for w in company.split() if len(w) > 2 and w not in {"sas", "ltda", "inc", "grupo", "the"}]
            if words:
                company_score = sum(w in body for w in words) / len(words) * 0.8

    title_score = 0.0
    job = [w for w in normalize_text(contact.title).split() if len(w) > 3]
    if job:
        title_score = sum(w in body for w in job) / len(job)

    return round(0.6 * name_score + 0.3 * company_score + 0.1 * title_score, 2)


class SerpApiLinkedInFinder:
    """Search-engine cascade through SerpAPI.

    Google currently ignores `site:` for many queries, so DuckDuckGo (which honours it) goes first and
    Google is the fallback. Only linkedin.com/in/ profile URLs are scored.
    """
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, min_confidence: float = 0.6, engines: tuple[str, ...] = ("duckduckgo", "google")):
        if not api_key:
            raise ValueError("SERPAPI_API_KEY no configurada")
        self.api_key = api_key
        self.min_confidence = min_confidence
        self.engines = engines

    async def _search(self, client: httpx.AsyncClient, q: str, engine: str = "google") -> list[dict]:
        params = {"engine": engine, "q": q, "api_key": self.api_key}
        if engine == "google":
            params.update({"num": 10, "hl": "es", "gl": "co"})
        r = await client.get(self.URL, params=params)
        r.raise_for_status()
        return [x for x in r.json().get("organic_results", []) if "linkedin.com/in/" in x.get("link", "")]

    def _queries(self, contact: Contact) -> list[str]:
        names = [contact.full_name]
        plain = normalize_text(contact.full_name).title()
        if plain != contact.full_name:
            names.append(plain)  # "Juan Sebastián Díaz" -> "Juan Sebastian Diaz"
        qs = []
        for n in names:
            if contact.company:
                qs.append(f'site:linkedin.com/in "{n}" {contact.company}')
            qs.append(f'site:linkedin.com/in "{n}"')
        return qs

    async def find(self, contact: Contact) -> LinkedInMatch | None:
        best: LinkedInMatch | None = None
        async with httpx.AsyncClient(timeout=30) as client:
            for engine in self.engines:
                for q in self._queries(contact):
                    try:
                        results = await self._search(client, q, engine)
                    except httpx.HTTPError as e:
                        log.warning("SerpAPI %s failed: %s", engine, e)
                        break
                    for res in results:
                        s = score_result(contact, res.get("title", ""), res.get("snippet", ""), res["link"])
                        if not best or s > best.confidence:
                            best = LinkedInMatch(url=res["link"].split("?")[0], confidence=s, title=res.get("title", ""))
                    if best and best.confidence >= self.min_confidence:
                        return best
        return best if best and best.confidence >= self.min_confidence else None


class MockLinkedInFinder:
    """Deterministic: finds a profile when the contact has a company."""

    async def find(self, contact: Contact) -> LinkedInMatch | None:
        if not contact.company:
            return None
        slug = normalize_text(contact.full_name).replace(" ", "-")
        return LinkedInMatch(
            url=f"https://www.linkedin.com/in/{slug}",
            confidence=0.92,
            title=f"{contact.full_name} - {contact.title} - {contact.company} | LinkedIn",
        )
