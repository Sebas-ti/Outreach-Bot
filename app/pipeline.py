"""Orchestrator: one sequential state machine per contact.

PENDING -> SEARCHING_LINKEDIN -> ENRICHING -> (NO_DATA | BLOCKED | CALLING)
CALLING -> MEETING_BOOKED | FOLLOW_UP_SENT | REFERRED | NOT_INTERESTED | NOT_SERIOUS | NO_ANSWER | CALL_DONE

Any failure in one record is logged and the pipeline moves on to the next one.
"""
import asyncio
import logging

from .config import Settings
from .db import Repository
from .models import CallOutcome, Contact, Status
from .providers.enrichment import Enricher
from .providers.linkedin import LinkedInFinder
from .providers.voice import VoiceProvider
from .utils import normalize_phone

log = logging.getLogger(__name__)

NO_ANSWER_REASONS = ("did-not-answer", "busy", "voicemail", "no-answer", "failed", "timeout")


def select_number(c: Contact, enriched_phone: str, s: Settings) -> tuple[str, str]:
    """Compliance guard. Returns (number_to_dial, explanation); empty number = do not call."""
    if s.voice_provider == "mock":
        return enriched_phone, "simulación: no se realiza ninguna llamada real"
    if not s.sandbox_mode:
        return enriched_phone, "producción: se marca el número enriquecido"
    consent = normalize_phone(c.consent_phone, s.default_country_code)
    if enriched_phone and enriched_phone in s.allowed_numbers:
        return enriched_phone, "sandbox: número en lista blanca"
    if consent:
        return consent, "sandbox: se marca el número de prueba con consentimiento"
    return "", "sandbox: el número no tiene consentimiento registrado, no se llama"


def outcome_status(current: Status, outcome: CallOutcome) -> Status:
    # tools (agendar / correo / referido) already set the definitive status during the call
    if current in (Status.MEETING_BOOKED, Status.FOLLOW_UP_SENT, Status.REFERRED, Status.NOT_SERIOUS):
        return current
    reason = (outcome.ended_reason or "").lower()
    if any(r in reason for r in NO_ANSWER_REASONS):
        return Status.NO_ANSWER
    resultado = (outcome.structured or {}).get("resultado", "")
    if resultado == "llamada_no_seria":
        return Status.NOT_SERIOUS
    if resultado in ("no_por_ahora", "no_contactar"):
        return Status.NOT_INTERESTED
    return Status.CALL_DONE


class Pipeline:
    def __init__(self, repo: Repository, linkedin: LinkedInFinder, enricher: Enricher,
                 voice: VoiceProvider, settings: Settings):
        self.repo, self.linkedin, self.enricher, self.voice, self.s = repo, linkedin, enricher, voice, settings
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._lock.locked()

    async def run(self, only_ids: set[int] | None = None) -> int:
        if self._lock.locked():
            return 0
        async with self._lock:
            pending = [c for c in self.repo.list(Status.PENDING) if only_ids is None or c.id in only_ids]
            self.repo.log(None, "pipeline", f"Inicio: {len(pending)} contactos pendientes")
            for c in pending:
                try:
                    await self.process(c)
                except Exception as e:
                    log.exception("contact %s failed", c.id)
                    self.repo.update(c.id, status=Status.ERROR)
                    self.repo.log(c.id, "error", f"{type(e).__name__}: {e}")
            self.repo.log(None, "pipeline", "Fin del lote")
            return len(pending)

    async def process(self, c: Contact) -> Status:
        # 1. LinkedIn
        self.repo.update(c.id, status=Status.SEARCHING_LINKEDIN)
        if c.linkedin_url:
            self.repo.log(c.id, "linkedin", f"URL proporcionada en la base: {c.linkedin_url}")
        else:
            match = await self.linkedin.find(c)
            if match:
                c = self.repo.update(c.id, linkedin_url=match.url, linkedin_confidence=match.confidence)
                self.repo.log(c.id, "linkedin", f"Perfil encontrado ({match.confidence:.0%}): {match.url}")
            else:
                self.repo.log(c.id, "linkedin", "Perfil no encontrado; se intenta enriquecer por email")

        # 2. Enrichment
        self.repo.update(c.id, status=Status.ENRICHING)
        self.repo.log(c.id, "enrichment", f"Consultando proveedores ({self.s.enrichment_provider})… "
                                         f"SignalHire puede tardar hasta {self.s.enrichment_timeout_s}s")
        res = await self.enricher.enrich(c)
        if res.email and not c.email:
            c = self.repo.update(c.id, email=res.email)
        if not res.found:
            self.repo.update(c.id, status=Status.NO_DATA)
            self.repo.log(c.id, "enrichment", f"Sin celular ({res.reason}) → siguiente registro")
            return Status.NO_DATA
        c = self.repo.update(c.id, phone=res.phone, phone_source=res.reason)
        self.repo.log(c.id, "enrichment", f"Celular encontrado vía {res.reason}: {mask(res.phone)}")

        # 3. Call (with compliance guard)
        number, why = select_number(c, res.phone, self.s)
        self.repo.log(c.id, "compliance", why)
        if not number:
            self.repo.update(c.id, status=Status.BLOCKED)
            return Status.BLOCKED
        call_id = await self.voice.call(c, number)
        self.repo.update(c.id, status=Status.CALLING, call_id=call_id)
        self.repo.log(c.id, "call", f"Llamando a {mask(number)} (call_id={call_id})")

        outcome = await self.voice.wait_outcome(call_id)
        c = self.repo.get(c.id)
        final = outcome_status(c.status, outcome)
        structured = dict(outcome.structured or {})
        if structured.get("resultado") == "reunion_agendada" and final != Status.MEETING_BOOKED:
            structured["resultado"] = "reunion_no_confirmada"  # the LLM said yes, the calendar did not
            self.repo.log(c.id, "call", "El análisis dice reunión, pero el calendario no la confirmó → revisar")
        self.repo.update(c.id, status=final, call_summary=outcome.summary, transcript=outcome.transcript,
                         recording_url=outcome.recording_url, ended_reason=outcome.ended_reason,
                         call_duration_s=outcome.duration_s, call_cost=outcome.cost,
                         qualification={**c.qualification, **structured})
        self.repo.log(c.id, "call", f"Llamada terminada ({outcome.ended_reason}) → {final}")
        return final


def mask(phone: str) -> str:
    return phone[:-4].rstrip()[:4] + "•••" + phone[-4:] if len(phone) > 6 else phone
