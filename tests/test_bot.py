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


# ---------------- persona: gender, language, barge-in
def test_gender_inference_and_opposite_agent():
    from app.persona import agent_persona, infer_gender
    assert infer_gender("María José") == "f" and infer_gender("José María") == "m"
    assert infer_gender("Andrés Felipe") == "m" and infer_gender("Isabel") == "f" and infer_gender("Kim") == ""
    s = Settings(_env_file=None, vapi_voice_id="voz-f", vapi_voice_id_male="voz-m")
    man, woman, unknown = (agent_persona(contact(first_name=n), s) for n in ("Andrés", "Laura", "Kim"))
    assert (man.name, man.gender, man.voice_id) == ("Sofía", "f", "voz-f")
    assert (woman.name, woman.gender, woman.voice_id) == ("Daniel", "m", "voz-m")
    assert unknown.gender == "f"  # default
    assert agent_persona(contact(first_name="Laura", gender="hombre"), s).gender == "f"  # CSV column wins
    assert agent_persona(contact(first_name="Laura"), Settings(_env_file=None, agent_gender_mode="female")).gender == "f"


def test_assistant_barge_in_multilingual_and_persona():
    a = build_assistant(contact(first_name="Laura"), S)
    assert a["firstMessageInterruptionsEnabled"] is True
    assert "espera" in a["stopSpeakingPlan"]["interruptionPhrases"]
    assert a["stopSpeakingPlan"]["numWords"] == 2  # background voices must not cut the agent
    assert "pero" not in a["stopSpeakingPlan"]["interruptionPhrases"]
    assert a["backgroundSpeechDenoisingPlan"]["smartDenoisingPlan"]["enabled"]
    assert a["transcriber"]["language"] == "multi" and "keyterm" in a["transcriber"]
    assert "language" not in a["voice"]  # ElevenLabs infers the language from the text
    assert a["endCallFunctionEnabled"] is False and "have a great day" in a["endCallPhrases"]
    prompt = a["model"]["messages"][0]["content"]
    assert a["firstMessage"].endswith("¿Hablo con Laura?")              # short greeting, waits for "¿Aló?"
    assert a["firstMessageMode"] == "assistant-waits-for-user"
    assert "Eres Daniel" in prompt and "Eres HOMBRE" in prompt and "Laura es mujer" in prompt
    assert "asistente virtual" in prompt and "NO frases para recitar" in prompt
    en = build_assistant(contact(first_name="John", language="en"), S)
    assert en["firstMessage"].startswith("Hi, good") and en["firstMessage"].endswith("Is this John?")
    assert en["model"]["tools"][0]["messages"][0]["content"].startswith("One moment")
    mono = build_assistant(contact(), Settings(_env_file=None, vapi_multilingual=False, vapi_transcriber_model="nova-2",
                                               vapi_voice_provider="11labs"))
    assert mono["transcriber"]["language"] == "es" and "keywords" in mono["transcriber"]
    assert mono["voice"]["language"] == "es"


def test_stand_notes_language_gender_columns_import():
    repo = Repository(":memory:")
    repo.import_csv("nombre,apellidos,idioma,genero,notas_stand\n"
                    "Ana,Uno,Portugués,F,Preguntó por servidores ThinkSystem para IA\n")
    c = repo.list()[0]
    assert (c.language, c.gender, c.stand_notes) == ("pt", "f", "Preguntó por servidores ThinkSystem para IA")
    from app.script import system_prompt
    assert "ThinkSystem" in system_prompt(c, S, datetime.now(S.tz))


def test_spoken_email_english_portuguese():
    from app.script import spoken_email
    assert spoken_email("jd2@x.co", "en") == "J D, two, at, X, dot, C O"
    assert spoken_email("ana@x.com", "pt") == "ana, arroba, xis, ponto, com"


# ---------------- tools: referral, title, escaping
def test_referral_becomes_pending_contact_and_title_is_updated(tmp_path, monkeypatch):
    from app.conversation import ConversationTools
    from app.providers.calendar import MockCalendarService

    monkeypatch.chdir(tmp_path)
    repo = Repository(":memory:")
    c = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme", title="Analista")
    cal = MockCalendarService(S.tz, 15, 9, 17, outbox=str(tmp_path / "out"))
    tools = ConversationTools(repo, cal, S)
    args = {"nombre": "Carlos Ruiz", "cargo": "CIO", "contacto": "carlos@acme.com"}
    asyncio.run(tools.dispatch("registrar_referido", args, c.id))
    asyncio.run(tools.dispatch("registrar_referido", args, c.id))  # no duplicates
    ref = [x for x in repo.list() if x.first_name == "Carlos"]
    assert len(ref) == 1 and ref[0].status == Status.PENDING and ref[0].company == "Acme"
    assert ref[0].email == "carlos@acme.com" and ref[0].consent_phone == ""  # never assumes consent
    assert repo.get(c.id).qualification["referido"]["contact_id"] == ref[0].id

    slot = asyncio.run(cal.free_slots(1))[0]
    asyncio.run(tools.dispatch("agendar_reunion", {"email": "ana@acme.com", "fecha_hora": slot.isoformat(),
                                                   "tema": "ia", "cargo": "Directora de TI"}, c.id))
    assert repo.get(c.id).title == "Directora de TI"


def test_follow_up_email_escapes_and_signs():
    from app.script import follow_up_email
    s = Settings(_env_file=None, sender_phone="+57 300 000 0000", sender_email="equipo@example.com")
    _, html = follow_up_email("<b>Ana</b>", "<script>x</script>", s)
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;Ana" in html
    assert "+57 300 000 0000 · equipo@example.com" in html


# ---------------- robustness
def test_webhook_tool_call_without_contact_does_not_500(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app(S, Repository(":memory:"))) as client:
        body = {"message": {"type": "tool-calls", "call": {"id": "desconocida"}, "toolCallList": [
            {"id": "t1", "function": {"name": "consultar_disponibilidad", "arguments": "{}"}}]}}
        r = client.post("/webhooks/vapi", json=body, headers={"x-vapi-secret": "s3cret"})
    assert r.status_code == 200 and "problema técnico" in r.json()["results"][0]["result"]


def test_await_registry_drops_stale_results_and_aliases(monkeypatch):
    from app.utils import AwaitRegistry

    async def scenario():
        reg = AwaitRegistry()
        reg.aliases["shreq:1"] = "sh:li:ana"
        reg.expect("sh:li:ana")
        reg.resolve("sh:li:ana", {"ok": 1})
        assert await reg.wait("sh:li:ana", 1) == {"ok": 1}
        assert "shreq:1" not in reg.aliases           # alias cleaned when its lookup finished
        reg.resolve("call:late", "report")            # late webhook nobody will wait for
        monkeypatch.setattr(AwaitRegistry, "EARLY_TTL_S", -1)
        reg.resolve("call:other", "x")
        assert "call:late" not in reg._early
    asyncio.run(scenario())


def test_retry_single_contact_keeps_the_rest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    repo = Repository(":memory:")
    a = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme", email="ana@example.com",
                         consent_phone="3001112233")
    b = repo.add_contact(first_name="Beto", last_name="Dos", company="Acme")
    repo.update(a.id, status=Status.NO_ANSWER, call_id="old")
    repo.update(b.id, status=Status.MEETING_BOOKED, meeting_start="2026-11-10T10:00:00-05:00")
    with TestClient(create_app(S, repo)) as client:
        assert client.post(f"/api/contacts/{a.id}/retry").json()["started"]
        for _ in range(100):
            time.sleep(0.05)
            if not client.get("/api/status").json()["running"]:
                break
    assert repo.get(a.id).status == Status.MEETING_BOOKED and repo.get(a.id).call_id != "old"
    assert repo.get(b.id).meeting_start == "2026-11-10T10:00:00-05:00"


def test_language_switch_needs_explicit_request_and_english_gets_native_voice():
    from app.persona import agent_persona
    s = Settings(_env_file=None, vapi_voice_id="es-f", vapi_voice_id_male="es-m",
                 vapi_voice_id_en="us-f", vapi_voice_id_en_male="us-m")
    assert agent_persona(contact(first_name="John", language="en"), s).voice_id == "us-f"   # man -> Sofía, US voice
    assert agent_persona(contact(first_name="Mary", language="en"), s).voice_id == "us-m"   # woman -> Daniel, US voice
    assert agent_persona(contact(first_name="Andrés"), s).voice_id == "es-f"                # Spanish stays Colombian
    assert agent_persona(contact(first_name="Ana", language="pt"), s).voice_id == "es-m"    # no pt voice -> fallback
    prompt = build_assistant(contact(first_name="Andrés"), s)["model"]["messages"][0]["content"]
    assert '"hello"' in prompt and "SOLO si la persona lo pide" in prompt


# ---------------- prank / abusive calls
def test_not_serious_call_is_flagged_blocks_actions_and_survives_outcome(tmp_path, monkeypatch):
    from app.conversation import ConversationTools
    from app.providers.calendar import MockCalendarService

    monkeypatch.chdir(tmp_path)
    repo = Repository(":memory:")
    c = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme")
    cal = MockCalendarService(S.tz, 15, 9, 17, outbox=str(tmp_path / "out"))
    tools = ConversationTools(repo, cal, S)
    r = asyncio.run(tools.dispatch("terminar_llamada_no_seria", {"motivo": "broma", "detalle": "NASA de Marte"}, c.id))
    assert r.startswith("Registrado") and repo.get(c.id).status == Status.NOT_SERIOUS
    assert repo.get(c.id).qualification["motivo_cierre"] == "broma"
    slot = asyncio.run(cal.free_slots(1))[0]
    r = asyncio.run(tools.dispatch("agendar_reunion", {"email": "a@b.co", "fecha_hora": slot.isoformat(), "tema": "ia"}, c.id))
    assert "no seria" in r and not cal.booked                       # nothing booked after flagging
    assert outcome_status(Status.NOT_SERIOUS, CallOutcome("x", "assistant-said-end-call-phrase")) == Status.NOT_SERIOUS
    assert outcome_status(Status.CALLING, CallOutcome("x", structured={"resultado": "llamada_no_seria"})) == Status.NOT_SERIOUS


def test_assistant_has_not_serious_tool_prompt_and_live_control():
    a = build_assistant(contact(), S)
    assert "terminar_llamada_no_seria" in {t["function"]["name"] for t in a["model"]["tools"]}
    assert a["monitorPlan"]["controlEnabled"] is True
    prompt = a["model"]["messages"][0]["content"]
    assert "LLAMADAS NO SERIAS" in prompt and "Ante la duda" in prompt


def test_mock_prank_scenario_end_to_end(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    repo = Repository(":memory:")
    with TestClient(create_app(S, repo)) as client:
        csv = "nombre,apellidos,empresa,email,telefono_consentido,escenario_demo\nBeto,Dos,Acme,b@example.com,3001112233,broma\n"
        client.post("/api/contacts/import", files={"file": ("c.csv", csv)})
        client.post("/api/pipeline/run")
        for _ in range(100):
            time.sleep(0.05)
            if not client.get("/api/status").json()["running"]:
                break
        beto = client.get("/api/contacts").json()[0]
    assert beto["status"] == "NOT_SERIOUS" and not beto["meeting_start"]


# ---------------- French + several attendees
def test_french_contact_gets_french_opening_voice_and_closing():
    from app.persona import agent_persona
    from app.script import spoken_email
    s = Settings(_env_file=None, vapi_voice_id_fr="fr-f", vapi_voice_id_fr_male="fr-m")
    man = contact(first_name="Ambroise", language="francés")
    a = build_assistant(man, s)
    assert agent_persona(man, s).voice_id == "fr-f"  # man -> Sofía with a native French voice
    assert a["firstMessage"].endswith("Je suis bien avec Ambroise ?")
    assert a["model"]["tools"][0]["messages"][0]["content"].startswith("Un instant")
    assert "une excellente journée" in a["endCallPhrases"]
    assert '"vous"' in a["model"]["messages"][0]["content"]
    assert "Eres Daniel" in build_assistant(contact(first_name="Claire", language="fr"), s)["model"]["messages"][0]["content"]
    assert spoken_email("ab2@x.fr", "fr") == "a bé, deux, arobase, ixe, point, effe erre"


def test_meeting_with_additional_attendees(tmp_path, monkeypatch):
    from app.conversation import ConversationTools
    from app.providers.calendar import MockCalendarService

    monkeypatch.chdir(tmp_path)
    repo = Repository(":memory:")
    c = repo.add_contact(first_name="Ana", last_name="Uno", company="Acme")
    cal = MockCalendarService(S.tz, 15, 9, 17, outbox=str(tmp_path / "out"))
    tools = ConversationTools(repo, cal, S)
    slot = asyncio.run(cal.free_slots(1))[0].isoformat()
    base = {"email": "ana@acme.com", "fecha_hora": slot, "tema": "ia"}
    bad = asyncio.run(tools.dispatch("agendar_reunion", {**base, "invitados_adicionales": [{"email": "carlos@"}]}, c.id))
    assert "invitado" in bad and not cal.booked
    guests = [{"nombre": "Carlos Ruiz", "email": "Carlos@Acme.com"}, {"email": "ana@acme.com"}, {"email": "lu@acme.com"}]
    ok = asyncio.run(tools.dispatch("agendar_reunion", {**base, "invitados_adicionales": guests}, c.id))
    assert "2 invitado(s)" in ok
    assert repo.get(c.id).qualification["invitados"] == ["Carlos Ruiz <carlos@acme.com>", "lu@acme.com"]
    invite = next((tmp_path / "out").glob("invite_*.txt")).read_text(encoding="utf-8")
    assert "carlos@acme.com" in invite and "lu@acme.com" in invite


def test_natural_voice_settings_and_speaks_first_fallback():
    a = build_assistant(contact(), Settings(_env_file=None, vapi_voice_provider="11labs"))
    assert a["voice"]["stability"] == 0.4 and a["model"]["temperature"] == 0.7
    b = build_assistant(contact(), Settings(_env_file=None, vapi_first_message_mode="assistant-speaks-first"))
    assert b["firstMessageMode"] == "assistant-speaks-first"


# ---------------- silence after picking up
def test_silence_nudge_only_when_nobody_speaks(monkeypatch):
    import types
    from app import main

    said = []

    async def fake_control(c, call, payload):
        said.append(payload["content"])
        return 200

    monkeypatch.setattr(main, "control", fake_control)
    s = Settings(_env_file=None, silence_nudge_after_s=0.01)
    c = types.SimpleNamespace(s=s, repo=types.SimpleNamespace(log=lambda *a: None))
    asyncio.run(main.silence_nudge(c, {"id": "quiet"}, 1, "es"))
    assert said == ["¿Aló? ¿Me escuchas?", "¿Aló? Creo que no te escucho bien."]
    said.clear()
    main.HEARD.add("talked")
    asyncio.run(main.silence_nudge(c, {"id": "talked"}, 1, "fr"))
    assert said == []
    main.HEARD.discard("talked")


def test_assistant_listens_for_speech_updates():
    assert "speech-update" in build_assistant(contact(), S)["serverMessages"]
