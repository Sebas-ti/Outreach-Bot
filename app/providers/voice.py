"""Stage 3 - Automated voice call.

Vapi runs the real-time loop (telephony + speech-to-text + LLM + text-to-speech).
We send a *transient* assistant per call (prompt personalised with the contact
data) and receive tool calls / the end-of-call report on /webhooks/vapi.
"""
import asyncio
import logging
from datetime import datetime
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..conversation import TOOL_SPECS, ConversationTools
from ..models import CallOutcome, Contact
from ..persona import (ACKNOWLEDGEMENT_PHRASES, END_CALL_PHRASES, IDLE_MESSAGES, INTERRUPTION_PHRASES,
                       WAIT_MESSAGES, Persona, agent_persona, first_message, voicemail_message)
from ..script import system_prompt
from ..utils import AwaitRegistry

log = logging.getLogger(__name__)

STRUCTURED_SCHEMA = {
    "type": "object",
    "properties": {
        "resultado": {"type": "string", "enum": [
            "reunion_agendada", "enviar_informacion", "no_por_ahora", "no_es_la_persona",
            "no_contactar", "llamada_incompleta", "llamada_no_seria"]},
        "tiene_proyecto": {"type": "boolean"},
        "descripcion_proyecto": {"type": "string"},
        "etapa": {"type": "string", "enum": ["exploracion", "evaluacion", "ejecucion", "desconocida"]},
        "fecha_meta": {"type": "string"},
        "lider_y_decisores": {"type": "string"},
        "tema_interes": {"type": "string", "enum": ["dispositivos", "infraestructura", "servicios", "ia", "otro", "ninguno"]},
        "objeciones": {"type": "array", "items": {"type": "string"}},
    },
}


class VoiceProvider(Protocol):
    async def call(self, contact: Contact, phone: str) -> str: ...
    async def wait_outcome(self, call_id: str) -> CallOutcome: ...


def build_voice(s: Settings, p: Persona | None = None) -> dict[str, Any]:
    voice: dict[str, Any] = {
        "provider": s.vapi_voice_provider,
        "voiceId": p.voice_id if p else s.vapi_voice_id,
        # Vapi's default formatter turns numbers into ENGLISH words ("two thousand...") -> disable it;
        # the prompt already forces numbers written in Spanish words.
        "chunkPlan": {"enabled": True, "formatPlan": {"enabled": False}},
    }
    if s.vapi_voice_model:
        voice["model"] = s.vapi_voice_model
    # Forcing a language would make the voice read English/Portuguese text with Spanish rules, so in
    # multilingual mode ElevenLabs infers it from the text; otherwise pin the opening language.
    if s.vapi_voice_provider in ("11labs", "cartesia") and not s.vapi_multilingual:
        voice["language"] = p.language if p else "es"
    return voice


def build_transcriber(s: Settings, p: Persona | None = None) -> dict[str, Any]:
    terms = [s.brand_name, s.event_name_spoken]
    tr: dict[str, Any] = {"provider": "deepgram", "model": s.vapi_transcriber_model,
                          "language": "multi" if s.vapi_multilingual else (p.language if p else "es")}
    if s.vapi_transcriber_model.startswith("nova-3"):
        tr["keyterm"] = terms  # nova-3: keyterm prompting (plain strings)
    else:
        tr["keywords"] = [f"{w}:2" for w in terms]  # nova-2: single words with boost
    return tr


def build_assistant(contact: Contact, s: Settings) -> dict[str, Any]:
    persona = agent_persona(contact, s)
    lang = persona.language
    server = {"url": f"{s.public_base_url}/webhooks/vapi", "headers": {"x-vapi-secret": s.webhook_secret}}
    tools = []
    for spec in TOOL_SPECS:
        tool: dict[str, Any] = {
            "type": "function",
            "function": {"name": spec["name"], "description": spec["description"], "parameters": spec["parameters"]},
            "server": server,
        }
        wait = WAIT_MESSAGES.get(spec["name"], {}).get(lang)
        if wait:
            tool["messages"] = [{"type": "request-start", "content": wait}]
        tools.append(tool)
    return {
        "name": f"Seguimiento {s.event_name}"[:40],
        "firstMessage": first_message(contact, s, persona),
        "firstMessageMode": "assistant-speaks-first",
        # Vapi's default is False: the person could not cut the (long) greeting -> "it keeps talking"
        "firstMessageInterruptionsEnabled": True,
        "model": {
            "provider": s.vapi_model_provider,
            "model": s.vapi_model,
            "temperature": 0.4,
            "messages": [{"role": "system", "content": system_prompt(contact, s, datetime.now(s.tz), persona)}],
            "tools": tools,
        },
        "voice": build_voice(s, persona),
        "transcriber": build_transcriber(s, persona),
        # latency: answer quickly after the person stops talking (default no-punctuation wait is 1.5s)
        "startSpeakingPlan": {
            "waitSeconds": s.vapi_wait_seconds,
            "transcriptionEndpointingPlan": {"onPunctuationSeconds": 0.1, "onNoPunctuationSeconds": 0.8,
                                             "onNumberSeconds": 0.5},
        },
        # if nobody talks, the agent re-engages instead of silently hanging up
        "messagePlan": {
            "idleMessages": IDLE_MESSAGES.get(lang, IDLE_MESSAGES["es"]),
            "idleTimeoutSeconds": 7,
            "idleMessageMaxSpokenCount": 3,
        },
        # barge-in: the agent stops when the person says `vapi_stop_num_words` transcribed words (not on any
        # sound: a neighbour talking or street noise used to cut it off). Explicit phrases ("espera", "perdón",
        # "wait") stop it instantly; pure backchannels ("ajá", "mjm") never do. Krisp removes background noise.
        "stopSpeakingPlan": {"numWords": s.vapi_stop_num_words, "voiceSeconds": 0.3, "backoffSeconds": 1,
                             "interruptionPhrases": INTERRUPTION_PHRASES,
                             "acknowledgementPhrases": ACKNOWLEDGEMENT_PHRASES},
        "backgroundSpeechDenoisingPlan": {"smartDenoisingPlan": {"enabled": True}},
        "backgroundSound": "off",
        # No endCall tool: the LLM used it to hang up right after booking, before saying the confirmation.
        # The call ends when the agent says the mandatory goodbye (any language), after confirming.
        "endCallFunctionEnabled": False,
        "endCallPhrases": END_CALL_PHRASES,
        "voicemailMessage": voicemail_message(contact, s, persona),
        "maxDurationSeconds": 480,
        # exposes call.monitor.controlUrl: lets the server end a prank/abusive call if the agent doesn't
        "monitorPlan": {"controlEnabled": True},
        "silenceTimeoutSeconds": 45,
        "server": server,
        "serverMessages": ["end-of-call-report", "status-update", "tool-calls"],
        "analysisPlan": {
            "summaryPlan": {"messages": [{"role": "system", "content":
                "Resume en español, en 3 frases, la llamada de seguimiento: interés, proyecto y próximo paso."}]},
            "structuredDataPlan": {"enabled": True, "schema": STRUCTURED_SCHEMA},
        },
        "metadata": {"contact_id": contact.id, "agent": persona.name, "language": lang},
    }


def parse_end_of_call(message: dict[str, Any]) -> CallOutcome:
    call = message.get("call") or {}
    artifact = message.get("artifact") or {}
    analysis = message.get("analysis") or call.get("analysis") or {}
    return CallOutcome(
        call_id=call.get("id", ""),
        ended_reason=message.get("endedReason") or call.get("endedReason", ""),
        summary=analysis.get("summary") or message.get("summary", ""),
        transcript=artifact.get("transcript") or message.get("transcript", ""),
        recording_url=artifact.get("recordingUrl") or message.get("recordingUrl", ""),
        structured=analysis.get("structuredData") or {},
        duration_s=_duration(message, call),
        cost=float(message.get("cost") or call.get("cost") or 0),
    )


def _duration(message: dict[str, Any], call: dict[str, Any]) -> float:
    if message.get("durationSeconds"):
        return float(message["durationSeconds"])
    start = message.get("startedAt") or call.get("startedAt")
    end = message.get("endedAt") or call.get("endedAt")
    if start and end:
        return max(0.0, (datetime.fromisoformat(end.replace("Z", "+00:00"))
                         - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    return 0.0


class VapiVoiceProvider:
    URL = "https://api.vapi.ai/call"

    def __init__(self, s: Settings, registry: AwaitRegistry):
        if not (s.vapi_api_key and s.vapi_phone_number_id):
            raise ValueError("VAPI_API_KEY y VAPI_PHONE_NUMBER_ID son obligatorios")
        self.s = s
        self.registry = registry

    async def preflight(self) -> None:
        """Never dial if Vapi could not reach our tools (dead ngrok tunnel -> tools would 404 mid-call)."""
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{self.s.public_base_url}/api/status")
            if r.status_code == 200 and "providers" in r.text:
                return
            reason = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            reason = type(e).__name__
        raise RuntimeError(f"El túnel público {self.s.public_base_url} no responde ({reason}). "
                           "¿Está corriendo ngrok? No se realizó la llamada.")

    async def call(self, contact: Contact, phone: str) -> str:
        await self.preflight()
        payload = {
            "phoneNumberId": self.s.vapi_phone_number_id,
            "customer": {"number": phone, "name": contact.full_name[:40]},
            "assistant": build_assistant(contact, self.s),
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(self.URL, json=payload,
                                  headers={"Authorization": f"Bearer {self.s.vapi_api_key}"})
        if r.status_code >= 400:
            raise RuntimeError(f"Vapi HTTP {r.status_code}: {r.text[:300]}")
        call_id = r.json()["id"]
        self.registry.expect(f"call:{call_id}")
        return call_id

    async def fetch_call(self, call_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{self.URL}/{call_id}",
                                 headers={"Authorization": f"Bearer {self.s.vapi_api_key}"})
        r.raise_for_status()
        return r.json()

    async def wait_outcome(self, call_id: str) -> CallOutcome:
        """Webhook first; every `poll` seconds fall back to asking Vapi (survives a dead tunnel)."""
        poll, waited, ended_seen = 15, 0, False
        while waited < self.s.call_timeout_s:
            try:
                return await self.registry.wait(f"call:{call_id}", poll)
            except TimeoutError:
                waited += poll
            try:
                call = await self.fetch_call(call_id)
            except httpx.HTTPError as e:
                log.warning("polling Vapi call %s failed: %s", call_id, e)
                continue
            if call.get("status") == "ended":
                # give the analysis (summary/structured data) one more cycle to be attached
                if call.get("analysis") or ended_seen:
                    log.warning("No webhook for call %s; outcome obtained by polling", call_id)
                    return parse_end_of_call({"call": call, "endedReason": call.get("endedReason", ""),
                                              "artifact": call.get("artifact") or {},
                                              "analysis": call.get("analysis") or {}})
                ended_seen = True
        return CallOutcome(call_id=call_id, ended_reason="timeout-waiting-report")


class MockVoiceProvider:
    """Simulates a conversation, invoking the SAME tools as the real agent.

    Scenario comes from the CSV column `escenario_demo`:
    acepta (default) | info | no_interesado | no_contesta | referido
    """

    def __init__(self, s: Settings, tools: ConversationTools, registry: AwaitRegistry):
        self.s = s
        self.tools = tools
        self.registry = registry
        self._n = 0

    async def call(self, contact: Contact, phone: str) -> str:
        self._n += 1
        call_id = f"mock-call-{contact.id}-{self._n}"
        self.registry.expect(f"call:{call_id}")
        asyncio.create_task(self._simulate(contact, call_id))
        return call_id

    async def _simulate(self, c: Contact, call_id: str) -> None:
        sc = (c.demo_scenario or "acepta").lower()
        await asyncio.sleep(self.s.mock_call_delay_s)
        email = c.email or f"{c.first_name.lower()}@example.com"
        lines = [f"AI: {first_message(c, self.s, agent_persona(c, self.s))}"]
        structured: dict[str, Any] = {}
        ended = "customer-ended-call"
        if sc == "no_contesta":
            ended, lines = "customer-did-not-answer", []
        elif sc == "acepta":
            lines += ["User: Sí, claro.", "AI: ¿Tienen algún proyecto de modernización tecnológica en evaluación o planeación?",
                      "User: Sí, estamos renovando los equipos de la fuerza comercial y evaluando IA en el contact center.",
                      "AI: ¿En qué etapa está?", "User: Evaluando opciones, queremos decidir antes de marzo.",
                      "AI: ¿Te parece si coordinamos 15 minutos con nuestro especialista?", "User: Dale."]
            slots = await self.tools.calendar.free_slots(3)
            await self.tools.dispatch("consultar_disponibilidad", {}, c.id)
            await self.tools.dispatch("agendar_reunion", {
                "email": email, "fecha_hora": slots[0].isoformat(), "tema": "dispositivos",
                "cargo": c.title, "notas": "Renovación de equipos fuerza comercial + IA en contact center. "
                                           "Etapa: evaluación. Meta: decidir antes de marzo."}, c.id)
            lines += ["AI: Listo, te llega la invitación hoy mismo. Gracias por acompañarnos en ANDICOM."]
            structured = {"resultado": "reunion_agendada", "tiene_proyecto": True, "etapa": "evaluacion",
                          "tema_interes": "dispositivos", "fecha_meta": "marzo"}
        elif sc == "info":
            lines += ["User: Envíame la información.", "AI: Con gusto, ¿qué tema te interesa más?", "User: Infraestructura."]
            await self.tools.dispatch("enviar_resumen_correo", {"email": email, "tema_interes": "infraestructura"}, c.id)
            structured = {"resultado": "enviar_informacion", "tiene_proyecto": False, "tema_interes": "infraestructura",
                          "objeciones": ["Envíame la información"]}
        elif sc == "broma":
            lines += ["User: Jajaja, sí, soy el presidente de la NASA y mi correo es noteimporta.",
                      "AI: Para no quitarte tiempo, ¿tienen algún proyecto de modernización?",
                      "User: Sí, una nave a Marte con computadores de papel, jajaja."]
            await self.tools.dispatch("terminar_llamada_no_seria",
                                      {"motivo": "broma", "detalle": "Respuestas absurdas y burlas"}, c.id)
            lines += ["AI: Entiendo, por ahora lo dejamos aquí. Gracias por tu tiempo. ¡Que tengas un excelente día!"]
            structured = {"resultado": "llamada_no_seria"}
        elif sc == "referido":
            lines += ["User: No soy la persona indicada, eso lo ve Carlos Ruiz, el CIO."]
            await self.tools.dispatch("registrar_referido", {"nombre": "Carlos Ruiz", "cargo": "CIO"}, c.id)
            structured = {"resultado": "no_es_la_persona"}
        else:
            lines += ["User: No, por ahora no tenemos nada.", "AI: Entiendo. ¿Te comparto un resumen por correo?",
                      "User: No, gracias."]
            structured = {"resultado": "no_por_ahora", "tiene_proyecto": False}
        self.registry.resolve(f"call:{call_id}", CallOutcome(
            call_id=call_id, ended_reason=ended, transcript="\n".join(lines),
            summary=f"[Simulación] escenario '{sc}'.", structured=structured,
            duration_s=0.0 if sc == "no_contesta" else 25.0 + 9 * len(lines)))

    async def wait_outcome(self, call_id: str) -> CallOutcome:
        try:
            return await self.registry.wait(f"call:{call_id}", self.s.call_timeout_s)
        except TimeoutError:
            return CallOutcome(call_id=call_id, ended_reason="timeout-waiting-report")
