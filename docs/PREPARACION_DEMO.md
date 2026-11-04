# Preparación de la demo en vivo – preguntas probables

## Arquitectura y criterio
**¿Por qué código y no n8n?**
Hay dos cosas asíncronas difíciles: el webhook de SignalHire y las herramientas que el agente invoca *durante* la llamada. En código quedan testeadas (19 pruebas), versionadas y detrás de interfaces (real/simulado). n8n sirve para prototipar o para que negocio dispare el lote llamando a `/api/pipeline/run`.

**¿Cómo está organizado?**
Una máquina de estados por contacto (`app/pipeline.py`). Proveedores intercambiables (`app/providers/`). Las herramientas de la llamada están en `app/conversation.py` y el guion del cliente vive como prompt en `app/script.py`. Cada paso queda registrado en la bitácora.

## Hallazgos reales de las pruebas (cuéntalos: muestran criterio)
- **Google ignoraba `site:linkedin.com/in`** y devolvía noticias. Probé Bing, Yahoo y DuckDuckGo: DuckDuckGo sí lo respeta, así que implementé una **cascada de buscadores**. Encontró el perfil real con 100 % en una sola búsqueda.
- **Mi propio perfil no está indexado en ningún buscador** (es nuevo). Por eso, si no hay perfil, el bot no se detiene: enriquece por email.
- **SignalHire devuelve la URL en otro formato** que la que se le envía. El primer intento con otra persona se quedaba esperando, y lo resolví emparejando por el `Request-Id` que trae su webhook y por el identificador del perfil.
- **Voz:** Vapi pasaba los números a palabras en inglés ("two thousand…"). Desactivé ese formateo y el prompt obliga a escribir fechas y horas en palabras en español. El correo se dicta así: "laura, uno, dos, tres, arroba…".
- **Latencia:** las métricas de Vapi mostraban 2 a 4 s por turno; bajé el endpointing de 1,5 s a 0,8 s y cambié a GPT-4.1-mini.
- **El agente esperaba el "chao" para colgar:** ahora usa `endCallPhrases` y cuelga apenas se despide.
- **El agente llegó a inventar una fecha de 2023** cuando falló una herramienta. Ahora el backend valida que la fecha sea futura, en horario laboral y libre en el calendario, y el prompt prohíbe confirmar algo que el sistema no confirmó.
- **Las grabaciones de Vapi son privadas:** el dashboard pide un link firmado temporal a la API de Vapi.

## Preguntas técnicas
**¿Cómo evitas llamar a la persona equivocada?**
Score de 0 a 1: nombre 60 % (exige nombre y apellido en el título del resultado), empresa 30 % y cargo 10 %, con umbral de 0,6. Los directorios de LinkedIn ("600+ perfiles de…") puntúan 0.

**¿Qué pasa si SignalHire no responde o no tiene el celular?**
Timeout de 90 s y estado `NO_DATA` con el motivo; el lote sigue. La cascada permite agregar Lusha o Apollo sin tocar el orquestador.

**¿Cómo se agenda en plena llamada?**
*Tool calling* de Vapi: `consultar_disponibilidad` revisa free/busy en Google Calendar y propone días distintos; `agendar_reunion` valida el correo, la fecha y los conflictos, y crea el evento con Meet y `sendUpdates=all`.

**¿Cómo controlas que el LLM no invente?**
Las acciones solo ocurren vía herramientas que valida el backend. Reglas explícitas en el prompt, temperatura 0,4, y el análisis post-llamada se contrasta con el calendario (si dice "reunión" pero no hubo evento, queda como "no confirmada").

**¿Y si se cae el túnel o el webhook?**
El bot verifica el túnel antes de marcar y, si un webhook no llega, consulta el estado de la llamada directamente a Vapi.

**Costos reales**
La llamada de 2 min 19 s costó US$ 0,20 (voz + LLM + telefonía). SerpAPI cuesta alrededor de US$ 0,01 por búsqueda y SignalHire cobra solo si encuentra contactos. El costo dominante en producción sería el enriquecimiento. La pestaña Analítica calcula el **costo por reunión**.

**¿Escala?**
La POC es secuencial a propósito. Para escalar: cola (Redis/Cloud Tasks), N workers según las líneas telefónicas, Postgres y Cloud Run. El `AwaitRegistry` pasaría a Redis pub/sub.

**Protección de datos**
Ley 1581/2012: finalidad, consentimiento y supresión. Sandbox por defecto, teléfonos enmascarados, webhooks autenticados con secreto, y datos personales fuera de git. El agente se identifica como IA y respeta el "no me llames".

**¿Qué harías con una semana más?**
CRM, reintentos con WhatsApp, número local +57, más proveedores en la cascada, evaluación automática de llamadas y A/B del guion.

## Checklist del día de la demo
1. ngrok + servidor arriba → `check_setup.py` en verde.
2. Créditos: SignalHire (`check_setup`), ElevenLabs y saldo de Twilio y Vapi.
3. CSV de la demo listo, con la persona con consentimiento disponible para contestar.
4. **Plan B:** cambia la etapa que falle a `mock` en `.env` y reinicia; el resto sigue en real. Ten el video como respaldo.
