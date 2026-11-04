# Outreach Bot – Arquitectura, herramientas y siguientes pasos
**Juan Sebastián Díaz Rey · POC para Attack · Caso: seguimiento a contactos de ANDICOM 2026 (Lenovo)**

## Arquitectura
```
CSV ─► Orquestador Python (FastAPI + SQLite) · máquina de estados por contacto · bitácora auditable · dashboard en vivo
  1. LinkedIn ──── SerpAPI en cascada (DuckDuckGo → Google, "site:linkedin.com/in") + score nombre/empresa/cargo (≥ 0.6)
  2. Celular ───── SignalHire (asíncrono por webhook, emparejado por Request-Id) en cascada; sin celular → siguiente registro
  3. Guarda ────── sandbox: solo se marcan números con consentimiento / lista blanca; túnel verificado antes de marcar
  4. Llamada ───── Vapi: Twilio + Deepgram (STT) + GPT-4.1-mini + ElevenLabs (TTS), asistente generado por contacto
                    └ herramientas en vivo: consultar_disponibilidad · agendar_reunion · enviar_resumen_correo · registrar_referido
  5. Agenda ────── Google Calendar (free/busy + evento con Meet + invitación) · Gmail (correo de seguimiento del guion)
  6. Cierre ────── transcripción, grabación, resumen, calificación estructurada, duración y costo → dashboard y analítica
```
Cada etapa es una interfaz con implementación **real** y **simulada**, configurable en `.env`. Así se prueba por partes, la demo no depende de terceros y cambiar de proveedor toca un solo archivo. 19 pruebas automáticas.

## Herramientas y por qué
| Necesidad | Elección | Por qué |
|---|---|---|
| Orquestación | Python + FastAPI | Control de webhooks asíncronos y de llamadas en vivo, código testeable y versionable. n8n sirve para prototipar, pero es frágil con callbacks y estado. |
| LinkedIn | SerpAPI (DuckDuckGo → Google) | Sin scraping ni login (respeta los términos de LinkedIn). En las pruebas, Google ignoró `site:`; DuckDuckGo encontró el perfil real con **100 %** en una búsqueda. |
| Celular | SignalHire + cascada | La herramienta sugerida, con cobertura en LATAM. La cascada permite sumar Lusha o Apollo sin tocar el orquestador. |
| Voz | Vapi + ElevenLabs | Interrupciones, detección de buzón, *tool calling* durante la llamada y análisis post-llamada. Se ajustó la latencia (endpointing de 0,8 s) y la pronunciación de fechas y correos. |
| Agenda | Google Calendar + Meet | Respeta la disponibilidad real, valida el horario y envía una invitación nativa. |

## Resultados de la prueba real (persona con consentimiento)
Perfil de LinkedIn encontrado (100 %) → SignalHire: perfil sin celular → cascada al número consentido → llamada de **2 min 19 s, US$ 0,20** → calificó proyecto, etapa, fecha y tema → reunión con Meet agendada e invitación recibida → el agente colgó solo.

## Limitaciones
- **Cobertura:** perfiles nuevos o con visibilidad restringida no están en ningún buscador, y SignalHire puede tener el perfil sin el celular. Se mitiga con la cascada y el enriquecimiento por email.
- **Telefonía:** número de EE. UU. (Twilio). En producción se necesita un número local +57 y registro del caller ID.
- **Escala:** llamadas secuenciales y SQLite de un solo nodo, suficiente para la POC.
- **Legal:** producción exige base legal documentada (Ley 1581/2012), horarios permitidos y lista de "no contactar". La POC solo marca números con consentimiento.

## Siguientes pasos
1. Integración con CRM (HubSpot/Salesforce): leer los leads del evento y escribir la calificación.
2. Reintentos inteligentes para "no contestó" y WhatsApp como canal de respaldo.
3. Cola + concurrencia (Redis/Cloud Tasks), Postgres y despliegue en Cloud Run.
4. Más proveedores en la cascada (Lusha/Apollo) y número local.
5. Evaluación automática de llamadas (cumplimiento del guion, tasa de agendamiento) y A/B de aperturas y voces.
