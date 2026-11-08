import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
import httpx
from fastapi.responses import FileResponse, RedirectResponse

from .config import Settings, get_settings
from .conversation import ConversationTools
from .db import Repository
from .persona import agent_persona
from .pipeline import Pipeline, mask
from .providers.calendar import GoogleCalendarService, MockCalendarService
from .providers.enrichment import MockEnricher, SignalHireEnricher, WaterfallEnricher, enrichment_key
from .providers.linkedin import MockLinkedInFinder, SerpApiLinkedInFinder
from .providers.voice import MockVoiceProvider, VapiVoiceProvider, parse_end_of_call
from .utils import AwaitRegistry, decode_csv, xlsx_to_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


class Container:
    """Wires the concrete providers chosen in .env (dependency injection by hand)."""

    def __init__(self, s: Settings, repo: Repository | None = None):
        self.s = s
        self.repo = repo or Repository(s.database_path)
        self.registry = AwaitRegistry()

        self.calendar = (GoogleCalendarService(s.google_credentials_file, s.google_token_file, s.google_calendar_id,
                                               s.tz, s.meeting_duration_min, s.business_hours_start, s.business_hours_end)
                         if s.calendar_provider == "google"
                         else MockCalendarService(s.tz, s.meeting_duration_min, s.business_hours_start, s.business_hours_end))
        self.tools = ConversationTools(self.repo, self.calendar, s)

        self.linkedin = (SerpApiLinkedInFinder(s.serpapi_api_key, s.linkedin_min_confidence)
                         if s.linkedin_provider == "serpapi" else MockLinkedInFinder())
        # ENRICHMENT_PROVIDER accepts a waterfall, e.g. "signalhire,mock"
        enrichers = []
        for name in [n.strip() for n in s.enrichment_provider.split(",") if n.strip()]:
            if name == "signalhire":
                enrichers.append((name, SignalHireEnricher(
                    s.signalhire_api_key, f"{s.public_base_url}/webhooks/signalhire?token={s.webhook_secret}",
                    self.registry, s.enrichment_timeout_s, s.default_country_code)))
            elif name == "mock":
                enrichers.append((name, MockEnricher(s.default_country_code)))
            else:
                raise ValueError(f"Proveedor de enriquecimiento desconocido: {name}")
        self.enricher = WaterfallEnricher(enrichers)
        self.voice = (VapiVoiceProvider(s, self.registry) if s.voice_provider == "vapi"
                      else MockVoiceProvider(s, self.tools, self.registry))
        self.pipeline = Pipeline(self.repo, self.linkedin, self.enricher, self.voice, s)


BACKGROUND: set[asyncio.Task] = set()  # keep references so pending hang-up tasks aren't garbage-collected


async def end_call_later(c: Container, call: dict, contact_id: int) -> None:
    """Backstop for prank/abusive calls: give the agent time for its goodbye, then end the call through
    Vapi's live-control URL if it is still up (the goodbye phrase normally hangs up first)."""
    await asyncio.sleep(c.s.abuse_hangup_after_s)
    if not isinstance(c.voice, VapiVoiceProvider) or not call.get("id"):
        return
    try:
        live = await c.voice.fetch_call(call["id"])
        if live.get("status") == "ended":
            return
        url = (live.get("monitor") or call.get("monitor") or {}).get("controlUrl")
        if not url:
            log.warning("call %s has no controlUrl; cannot force hang-up", call["id"])
            return
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={"type": "end-call"})
        c.repo.log(contact_id, "call", f"Llamada no seria: colgada por el sistema (HTTP {r.status_code})")
    except Exception as e:  # never let the backstop crash the server
        log.warning("forced hang-up of %s failed: %s", call.get("id"), e)


def create_app(settings: Settings | None = None, repo: Repository | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.c = Container(settings or get_settings(), repo)
        s = app.state.c.s
        log.info("Providers: linkedin=%s enrichment=%s voice=%s calendar=%s sandbox=%s",
                 s.linkedin_provider, s.enrichment_provider, s.voice_provider, s.calendar_provider, s.sandbox_mode)
        yield

    app = FastAPI(title="Outreach Bot – ANDICOM follow-up", lifespan=lifespan)

    def C(request: Request) -> Container:
        return request.app.state.c

    # ------------------------------------------------------------------ UI
    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    # ------------------------------------------------------------------ API
    @app.get("/api/status")
    async def status(request: Request):
        s = C(request).s
        return {"running": C(request).pipeline.running, "sandbox": s.sandbox_mode, "providers": {
            "linkedin": s.linkedin_provider, "enrichment": s.enrichment_provider,
            "voice": s.voice_provider, "calendar": s.calendar_provider}}

    @app.get("/api/contacts")
    async def contacts(request: Request):
        out = []
        for c in C(request).repo.list():
            d = asdict(c)
            d["phone"] = mask(c.phone)
            d["consent_phone"] = mask(c.consent_phone)
            p = agent_persona(c, C(request).s)
            d.update(agent=p.name, agent_gender=p.gender, agent_language=p.language)
            out.append(d)
        return out

    @app.get("/api/events")
    async def events(request: Request, limit: int = 200, contact_id: int | None = None):
        return C(request).repo.events(limit, contact_id)

    @app.get("/api/contacts/{contact_id}/recording")
    async def recording(request: Request, contact_id: int):
        """Vapi recordings live in private storage: ask Vapi (private key) for a short-lived signed URL."""
        c = C(request)
        contact = c.repo.get(contact_id)
        if not contact or not contact.call_id or contact.call_id.startswith("mock"):
            raise HTTPException(404, "Esta llamada no tiene grabación")
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"https://api.vapi.ai/call/{contact.call_id}/mono-recording",
                                 headers={"Authorization": f"Bearer {c.s.vapi_api_key}"})
        if r.status_code in (301, 302, 303, 307) and r.headers.get("location"):
            return RedirectResponse(r.headers["location"])
        raise HTTPException(502, f"Vapi no entregó la grabación (HTTP {r.status_code})")

    @app.post("/api/contacts/import")
    async def import_contacts(request: Request, file: UploadFile = File(...), replace: bool = True):
        raw = await file.read()
        text = xlsx_to_csv(raw) if (file.filename or "").lower().endswith((".xlsx", ".xlsm")) else decode_csv(raw)
        if replace:
            C(request).repo.delete_all()
        try:
            n = C(request).repo.import_csv(text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        C(request).repo.log(None, "import", f"{n} contactos importados desde {file.filename}")
        return {"imported": n}

    @app.post("/api/contacts/load-sample")
    async def load_sample(request: Request):
        path = Path("data/contacts.csv")
        C(request).repo.delete_all()
        n = C(request).repo.import_csv(decode_csv(path.read_bytes()))
        C(request).repo.log(None, "import", f"{n} contactos importados desde {path}")
        return {"imported": n}

    @app.post("/api/pipeline/run")
    async def run_pipeline(request: Request):
        c = C(request)
        if c.pipeline.running:
            return {"started": False, "reason": "ya hay un lote en ejecución"}
        asyncio.create_task(c.pipeline.run())
        return {"started": True}

    @app.post("/api/contacts/{contact_id}/retry")
    async def retry_contact(request: Request, contact_id: int):
        """Re-run ONE contact (e.g. the demo call failed) without touching the rest of the batch."""
        c = C(request)
        if c.pipeline.running:
            raise HTTPException(409, "Hay un lote en ejecución; espera a que termine")
        contact = c.repo.get(contact_id)
        if not contact:
            raise HTTPException(404, "Contacto no encontrado")
        c.repo.reset_one(contact_id)
        c.repo.log(contact_id, "pipeline", f"Reintento manual de {contact.full_name}")
        asyncio.create_task(c.pipeline.run({contact_id}))
        return {"started": True}

    @app.post("/api/contacts/reset")
    async def reset(request: Request):
        C(request).repo.reset_all()
        return {"ok": True}

    # ------------------------------------------------------------------ webhooks
    @app.post("/webhooks/signalhire")
    async def signalhire_webhook(request: Request, token: str = Query("")):
        c = C(request)
        if token != c.s.webhook_secret:
            raise HTTPException(401, "invalid token")
        payload = await request.json()
        items = payload if isinstance(payload, list) else [payload]
        rid = request.headers.get("Request-Id", "")
        for entry in items:
            key = f"sh:{enrichment_key(entry.get('item', ''))}"
            if not c.registry.is_pending(key):
                alias = c.registry.aliases.pop(f"shreq:{rid}", None) if rid else None
                pending = c.registry.pending("sh:")
                if alias:
                    key = alias
                elif len(pending) == 1:  # sequential pipeline: only one lookup can be in flight
                    key = pending[0]
            log.info("SignalHire callback item=%s rid=%s status=%s -> %s", entry.get("item"), rid, entry.get("status"), key)
            c.registry.resolve(key, entry)
        return {"ok": True}

    @app.post("/webhooks/vapi")
    async def vapi_webhook(request: Request, x_vapi_secret: str = Header("")):
        c = C(request)
        if x_vapi_secret != c.s.webhook_secret:
            raise HTTPException(401, "invalid secret")
        body = await request.json()
        msg = body.get("message", {})
        mtype = msg.get("type")
        call = msg.get("call") or {}
        contact = c.repo.get_by_call_id(call.get("id", ""))
        contact_id = contact.id if contact else (call.get("assistant", {}).get("metadata", {}) or {}).get("contact_id")

        if mtype == "tool-calls":
            results = []
            for tc in msg.get("toolCallList") or msg.get("toolCalls") or []:
                fn = tc.get("function", {})
                try:
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        args = json.loads(args or "{}")
                    if not str(contact_id or "").isdigit() or not c.repo.get(int(contact_id)):
                        raise LookupError(f"llamada {call.get('id')} sin contacto asociado")
                    result = await c.tools.dispatch(fn.get("name", ""), args, int(contact_id))
                    if fn.get("name") == "terminar_llamada_no_seria" and result.startswith("Registrado"):
                        task = asyncio.create_task(end_call_later(c, call, int(contact_id)))
                        BACKGROUND.add(task)
                        task.add_done_callback(BACKGROUND.discard)
                except (LookupError, ValueError) as e:  # never answer 500 to Vapi in the middle of a call
                    log.warning("tool-call %s not dispatched: %s", fn.get("name"), e)
                    result = ("Hubo un problema técnico. Dile a la persona que el equipo le confirmará por correo "
                              "y no confirmes nada.")
                results.append({"toolCallId": tc.get("id"), "result": result})
            return {"results": results}

        if mtype == "status-update":
            c.repo.log(contact_id, "call", f"Vapi status: {msg.get('status')}")
        elif mtype == "end-of-call-report":
            outcome = parse_end_of_call(msg)
            c.registry.resolve(f"call:{outcome.call_id}", outcome)
        return {}

    return app


app = create_app()
