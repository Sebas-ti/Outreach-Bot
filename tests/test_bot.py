import asyncio
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Repository
from app.main import create_app
from app.models import CallOutcome, Contact, Status
from app.pipeline import outcome_status, select_number
from app.providers.calendar import candidate_slots, overlaps, pick_spread
from app.providers.enrichment import parse_signalhire_item
from app.providers.linkedin import score_result
from app.providers.voice import build_assistant, parse_end_of_call
from app.utils import normalize_phone

S = Settings(_env_file=None, database_path=":memory:", mock_call_delay_s=0.05, webhook_secret="s3cret")


def contact(**kw) -> Contact:
    base = dict(id=1, first_name="Laura", last_name="Gómez", company="Banco Andino", title="Directora de TI",
                email="laura@example.com")
    return Contact(**{**base, **kw})


# ---------------- utils
def test_normalize_phone():
    assert normalize_phone("300 123 4567") == "+573001234567"
    assert normalize_phone("+57 (300) 123-4567") == "+573001234567"
    assert normalize_phone("573001234567") == "+573001234567"
    assert normalize_phone("abc") == ""


def test_csv_import_tolerates_excel_formats():
    repo = Repository(":memory:")
    assert repo.import_csv("nombre,apellidos,escenario_demo;;;;\nAna,Uno,acepta;;;;\n;;;;\n") == 1
    assert repo.list()[0].demo_scenario == "acepta"
    repo = Repository(":memory:")
    assert repo.import_csv("﻿nombre;apellidos;empresa\nBeto;Dos;Acme, S.A.\n") == 1
    assert repo.list()[0].company == "Acme, S.A."


# ---------------- linkedin
def test_linkedin_scoring_prefers_right_person():
    c = contact()
    good = score_result(c, "Laura Gómez - Directora de TI - Banco Andino | LinkedIn", "", "https://co.linkedin.com/in/lauragomez")
    other_company = score_result(c, "Laura Gómez - Abogada - Bufete XYZ | LinkedIn", "", "https://linkedin.com/in/lg2")
    other_person = score_result(c, "Pedro Pérez - Banco Andino | LinkedIn", "", "https://linkedin.com/in/pp")
    not_profile = score_result(c, "Laura Gómez Banco Andino", "", "https://linkedin.com/company/banco")
    assert good >= 0.9
    assert good > other_company > other_person
    assert not_profile == 0


# ---------------- enrichment
def test_signalhire_parsing_prefers_mobile():
    entry = {"item": "https://linkedin.com/in/x", "status": "success", "candidate": {"contacts": [
        {"type": "phone", "value": "+57 601 555 0000", "subType": "work_phone", "rating": "100"},
        {"type": "phone", "value": "300 111 2222", "subType": "mobile", "rating": "80"},
        {"type": "email", "value": "x@corp.com"}]}}
    r = parse_signalhire_item(entry)
    assert r.found and r.phone == "+573001112222" and r.email == "x@corp.com"
    assert not parse_signalhire_item({"status": "failed"}).found
    assert not parse_signalhire_item({"status": "success", "candidate": {"contacts": []}}).found


def test_waterfall_falls_through_and_survives_errors():
    from app.models import EnrichmentResult
    from app.providers.enrichment import WaterfallEnricher

    class Boom:
        async def enrich(self, c): raise RuntimeError("down")

    class Empty:
        async def enrich(self, c): return EnrichmentResult(found=False, email="x@y.com", reason="nada")

    class Hit:
        async def enrich(self, c): return EnrichmentResult(found=True, phone="+573001112222")

    r = asyncio.run(WaterfallEnricher([("a", Boom()), ("b", Empty()), ("c", Hit())]).enrich(contact()))
    assert r.found and r.reason.startswith("c (tras") and "b: nada" in r.reason and r.email == "x@y.com"
    r = asyncio.run(WaterfallEnricher([("a", Boom()), ("b", Empty())]).enrich(contact()))
    assert not r.found and "a:" in r.reason and "b: nada" in r.reason


# ---------------- compliance
def test_sandbox_guard():
    s = Settings(_env_file=None, voice_provider="vapi", sandbox_mode=True, allowed_phone_numbers="+573009999999")
    assert select_number(contact(), "+573001112222", s)[0] == ""  # no consent -> blocked
    assert select_number(contact(consent_phone="3005556666"), "+573001112222", s)[0] == "+573005556666"
    assert select_number(contact(), "+573009999999", s)[0] == "+573009999999"
    prod = Settings(_env_file=None, voice_provider="vapi", sandbox_mode=False)
    assert select_number(contact(), "+573001112222", prod)[0] == "+573001112222"


def test_outcome_status():
    assert outcome_status(Status.CALLING, CallOutcome("1", ended_reason="customer-did-not-answer")) == Status.NO_ANSWER
    assert outcome_status(Status.MEETING_BOOKED, CallOutcome("1", ended_reason="customer-ended-call")) == Status.MEETING_BOOKED
    assert outcome_status(Status.CALLING, CallOutcome("1", structured={"resultado": "no_por_ahora"})) == Status.NOT_INTERESTED


# ---------------- calendar
def test_slots_business_hours_and_busy():
    tz = S.tz
    friday_4pm = datetime(2026, 11, 6, 16, 0, tzinfo=tz)
    slots = candidate_slots(friday_4pm, tz, 15, 9, 17)
    assert slots and all(s.weekday() < 5 and 9 <= s.hour < 17 for s in slots)
    assert slots[0] >= friday_4pm + timedelta(hours=2)
    spread = pick_spread(slots, 3)
    assert len({s.date() for s in spread}) == 3
    busy = [(slots[0], slots[0] + timedelta(minutes=30))]
    assert overlaps(slots[0], timedelta(minutes=15), busy)
    assert not overlaps(slots[0] + timedelta(minutes=30), timedelta(minutes=15), busy)


# ---------------- vapi payloads
def test_build_assistant_has_tools_and_secret():
    a = build_assistant(contact(), S)
    names = {t["function"]["name"] for t in a["model"]["tools"]}
    assert {"consultar_disponibilidad", "agendar_reunion", "enviar_resumen_correo", "registrar_referido"} <= names
    assert a["server"]["headers"]["x-vapi-secret"] == "s3cret"
    assert "Laura" in a["firstMessage"] and "Andicom" in a["model"]["messages"][0]["content"]


def test_parse_end_of_call():
    o = parse_end_of_call({"type": "end-of-call-report", "endedReason": "customer-ended-call",
                           "call": {"id": "c1"}, "artifact": {"transcript": "AI: hola", "recordingUrl": "u"},
                           "analysis": {"summary": "ok", "structuredData": {"resultado": "reunion_agendada"}}})
    assert o.call_id == "c1" and o.transcript == "AI: hola" and o.structured["resultado"] == "reunion_agendada"


# ---------------- end to end (mock providers, real orchestration + API)
def test_end_to_end_mock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    repo = Repository(":memory:")
    app = create_app(S, repo)
    csv = ("nombre,apellidos,empresa,cargo,email,telefono_consentido,escenario_demo\n"
           "Ana,Uno,Acme,CIO,ana@example.com,3001112233,acepta\n"
           "Beto,Dos,Acme,CTO,beto@example.com,,info\n"
           "Caro,Tres,,Consultora,,,\n"
           "Dani,Cuatro,Acme,CFO,dani@example.com,,no_contesta\n")
    with TestClient(app) as client:
        assert client.post("/api/contacts/import", files={"file": ("c.csv", csv)}).json() == {"imported": 4}
        assert client.post("/api/pipeline/run").json()["started"]
        for _ in range(100):
            time.sleep(0.05)
            if not client.get("/api/status").json()["running"]:
                break
        by_name = {c["first_name"]: c for c in client.get("/api/contacts").json()}
    assert by_name["Ana"]["status"] == "MEETING_BOOKED" and by_name["Ana"]["meeting_link"]
    assert by_name["Beto"]["status"] == "FOLLOW_UP_SENT"
    assert by_name["Caro"]["status"] == "NO_DATA"
    assert by_name["Dani"]["status"] == "NO_ANSWER"
    assert by_name["Ana"]["phone"].endswith("2233") and "•" in by_name["Ana"]["phone"]


def test_vapi_webhook_rejects_bad_secret_and_runs_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = Repository(":memory:")
    c = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme", email="ana@example.com")
    repo.update(c.id, call_id="call-xyz")
    with TestClient(create_app(S, repo)) as client:
        assert client.post("/webhooks/vapi", json={"message": {}}).status_code == 401
        body = {"message": {"type": "tool-calls", "call": {"id": "call-xyz"}, "toolCallList": [
            {"id": "t1", "function": {"name": "consultar_disponibilidad", "arguments": "{}"}}]}}
        r = client.post("/webhooks/vapi", json=body, headers={"x-vapi-secret": "s3cret"}).json()
        assert r["results"][0]["toolCallId"] == "t1" and "Horarios disponibles" in r["results"][0]["result"]


def test_vapi_polling_fallback_when_webhook_never_arrives(monkeypatch):
    from app.providers.voice import VapiVoiceProvider
    from app.utils import AwaitRegistry

    s = Settings(_env_file=None, vapi_api_key="k", vapi_phone_number_id="p", call_timeout_s=120)
    v = VapiVoiceProvider(s, AwaitRegistry())

    async def no_wait(key, timeout):  # webhook never arrives
        raise TimeoutError

    async def fake_fetch(call_id):
        return {"id": call_id, "status": "ended", "endedReason": "pipeline-error-eleven-labs-voice-failed",
                "analysis": {"summary": "x"}}

    monkeypatch.setattr(v.registry, "wait", no_wait)
    monkeypatch.setattr(v, "fetch_call", fake_fetch)
    o = asyncio.run(v.wait_outcome("c1"))
    assert o.call_id == "c1" and o.ended_reason == "pipeline-error-eleven-labs-voice-failed"


def test_agendar_validates_year_hours_and_conflicts(tmp_path, monkeypatch):
    from app.conversation import ConversationTools
    from app.providers.calendar import MockCalendarService

    monkeypatch.chdir(tmp_path)
    repo = Repository(":memory:")
    c = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme")
    cal = MockCalendarService(S.tz, 15, 9, 17, outbox=str(tmp_path / "out"))
    tools = ConversationTools(repo, cal, S)
    run = lambda fh: asyncio.run(tools.dispatch("agendar_reunion", {"email": "ana@example.com", "fecha_hora": fh, "tema": "ia"}, c.id))

    assert "ya pasó" in run("2023-11-05T15:00:00-05:00")          # LLM hallucinated year
    slot = asyncio.run(cal.free_slots(1))[0]
    saturday = slot + timedelta(days=(5 - slot.weekday()) % 7 or 7)
    assert "No se agendó" in run(saturday.replace(hour=10).isoformat())
    assert "No se agendó" in run(slot.replace(hour=20).isoformat())  # outside business hours
    assert "Reunión agendada" in run(slot.isoformat())
    assert repo.get(c.id).status == Status.MEETING_BOOKED
    assert "No se agendó" in run(slot.isoformat())                    # double booking


def test_vapi_does_not_dial_when_tunnel_is_down():
    import pytest
    from app.providers.voice import VapiVoiceProvider
    from app.utils import AwaitRegistry

    s = Settings(_env_file=None, vapi_api_key="k", vapi_phone_number_id="p",
                 public_base_url="https://127.0.0.1:9")  # nothing listens here
    with pytest.raises(RuntimeError, match="no responde"):
        asyncio.run(VapiVoiceProvider(s, AwaitRegistry()).call(contact(), "+573001112222"))


def test_ansi_csv_and_non_ascii_linkedin_url():
    from app.utils import decode_csv
    raw = "nombre,apellidos,linkedin_url\nJuan Sebastián,Díaz,https://www.linkedin.com/in/juan-sebastián-diaz\n".encode("cp1252")
    repo = Repository(":memory:")
    assert repo.import_csv(decode_csv(raw)) == 1
    c = repo.list()[0]
    assert c.first_name == "Juan Sebastián"
    assert c.linkedin_url == "https://www.linkedin.com/in/juan-sebasti%C3%A1n-diaz"


def test_spoken_email_for_tts():
    from app.script import spoken_email
    assert spoken_email("laura123@gmail.com") == "laura, uno, dos, tres, arroba, gmail, punto, com"
    assert spoken_email("jd.rey@x.co").startswith("jota de, punto, rey, arroba")


def test_signalhire_keys_match_across_url_formats():
    from app.providers.enrichment import canonical_linkedin, enrichment_key
    found = "https://co.linkedin.com/in/andr%C3%A9s-felipe-d%C3%ADaz-rey-4a27a422b"
    variants = [found, "https://www.linkedin.com/in/andrés-felipe-díaz-rey-4a27a422b/",
                "http://linkedin.com/in/Andr%c3%a9s-Felipe-D%c3%adaz-Rey-4a27a422b?trk=x"]
    assert len({enrichment_key(v) for v in variants}) == 1
    assert canonical_linkedin(found) == "https://www.linkedin.com/in/andr%C3%A9s-felipe-d%C3%ADaz-rey-4a27a422b"


def test_signalhire_callback_resolved_by_request_id_or_single_pending(tmp_path, monkeypatch):
    """The callback 'item' may differ from what we sent: Request-Id (or the only pending lookup) still matches."""
    monkeypatch.chdir(tmp_path)
    from app.main import Container
    from app.providers.enrichment import enrichment_key

    async def scenario():
        cont = Container(S, Repository(":memory:"))
        app = create_app(S, cont.repo)
        app.state.c = cont
        key = f"sh:{enrichment_key('https://www.linkedin.com/in/hermano-123')}"
        cont.registry.expect(key)
        cont.registry.aliases["shreq:77"] = key
        import httpx as hx
        transport = hx.ASGITransport(app=app)
        async with hx.AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.post(f"/webhooks/signalhire?token={S.webhook_secret}", headers={"Request-Id": "77"},
                                  json=[{"item": "https://www.linkedin.com/in/otro-formato", "status": "success",
                                         "candidate": {"contacts": [{"type": "phone", "value": "+57 300 111 2222", "subType": "mobile"}]}}])
            assert r.status_code == 200
        return await cont.registry.wait(key, 1)

    entry = asyncio.run(scenario())
    assert entry["status"] == "success"


def test_xlsx_import_first_sheet_keeps_plus_and_accents():
    import io
    from openpyxl import Workbook
    from app.utils import xlsx_to_csv
    wb = Workbook(); ws = wb.active
    ws.append(["nombre", "apellidos", "telefono_consentido", "resultado_esperado"])
    ws.append(["José", "Peña", "+573001112233", "ignorada por el bot"])
    wb.create_sheet("Instrucciones").append(["no se importa"])
    buf = io.BytesIO(); wb.save(buf)
    repo = Repository(":memory:")
    assert repo.import_csv(xlsx_to_csv(buf.getvalue())) == 1
    c = repo.list()[0]
    assert (c.first_name, c.last_name, c.consent_phone) == ("José", "Peña", "+573001112233")
