# Outreach Bot · Seguimiento post-ANDICOM

Bot que toma una base de contactos (nombre, apellidos, empresa, cargo…) y, por cada registro:

1. **Identifica su perfil de LinkedIn** (búsqueda pública `site:linkedin.com/in` en cascada DuckDuckGo → Google + score de coincidencia).
2. **Enriquece el celular** con SignalHire (cascada de proveedores). Sin datos → **pasa al siguiente**.
3. **Llama** con un agente de voz IA (Vapi + ElevenLabs) que sigue el guion de Lenovo/ANDICOM, califica y maneja objeciones.
4. **Agenda** una reunión de 15 min en Google Calendar con link de Meet y **envía la invitación** al correo,
   o envía el correo de seguimiento del guion si la persona prefiere información.

Todo queda auditado en un dashboard en vivo con tres secciones: **Pipeline** (embudo, progreso por contacto, llamada en curso, detalle con grabación y transcripción), **Analítica** (tasas de contacto y agendamiento, costo por reunión, gráficas) y **Logs** (filtros y exportación a CSV).

📄 Documento de 1 página: [`docs/Arquitectura_OutreachBot.pdf`](docs/Arquitectura_OutreachBot.pdf) · [Word](docs/Arquitectura_OutreachBot.docx) · [Markdown](docs/ARQUITECTURA.md)

```
CSV ─► Orquestador (FastAPI + SQLite, máquina de estados por contacto)
         │ 1 SerpAPI ──────────► perfil LinkedIn (score ≥ 0.6)
         │ 2 SignalHire ◄─webhook─ celular (cascada; si no hay → siguiente)
         │ 3 Guarda de cumplimiento (sandbox / consentimiento)
         │ 4 Vapi ─── llamada ──► persona
         │      ◄── tool-calls ── consultar_disponibilidad / agendar_reunion / enviar_resumen_correo / registrar_referido
         │      ◄── end-of-call ─ transcripción + resumen + datos estructurados
         └ 5 Google Calendar (Meet + invitación)  ·  Gmail (seguimiento)
```

## Arranque rápido (modo simulado, sin cuentas)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate en mac/linux)
pip install -r requirements.txt
copy .env.example .env           # deja todo en mock
uvicorn app.main:app --reload
```
Abre http://localhost:8000 → **Cargar base de ejemplo** → **▶ Ejecutar pipeline**.
Tests: `pytest` (19 pruebas: scoring LinkedIn, parsing y emparejamiento de SignalHire, cascadas, guarda de cumplimiento, validación de horarios, webhooks, respaldo por consulta a Vapi, end-to-end).

## Modo real (paso a paso)

Cada etapa se activa por separado en `.env`, así puedes probar una a la vez.

### 0. Túnel público (para los webhooks de Vapi y SignalHire)
Instala [ngrok](https://ngrok.com/download), luego `ngrok http 8000` y copia la URL https en `PUBLIC_BASE_URL`.
Pon un valor largo y aleatorio en `WEBHOOK_SECRET`.

### 1. LinkedIn → SerpAPI
Crea una cuenta en serpapi.com (plan gratis) → copia la API key → `SERPAPI_API_KEY`, `LINKEDIN_PROVIDER=serpapi`.

### 2. Celular → SignalHire
Crea una cuenta en signalhire.com → *Integrations / API* → copia la API key → `SIGNALHIRE_API_KEY`.
Usa `ENRICHMENT_PROVIDER=signalhire` (o `signalhire,mock` para la cascada en la demo).
SignalHire responde de forma **asíncrona** en `/webhooks/signalhire`, por eso necesita el túnel.

### 3. Llamada → Vapi (+ número Twilio para llamar a Colombia)
1. dashboard.vapi.ai → *API Keys* → **Private key** → `VAPI_API_KEY`.
2. Los números gratis de Vapi solo llaman a EE. UU. Para llamar a un celular colombiano:
   crea una cuenta trial en Twilio → compra un número (sale del crédito de prueba) →
   *Verified Caller IDs*: verifica **tu** celular → *Voice Geo Permissions*: habilita **Colombia**.
3. En Vapi → *Phone Numbers* → *Import* → Twilio (Account SID + Auth Token + número) → copia el **ID** → `VAPI_PHONE_NUMBER_ID`.
4. `VOICE_PROVIDER=vapi`. El asistente se crea **por llamada** desde el código (`app/providers/voice.py`), no hay que configurarlo en el dashboard.
5. *(Opcional)* Voz más natural: conecta tu API key de ElevenLabs en Vapi (*Integrations*) y usa `VAPI_VOICE_PROVIDER=11labs`, `VAPI_VOICE_ID=<id>`, `VAPI_VOICE_MODEL=eleven_flash_v2_5`. Las voces de la biblioteca de ElevenLabs requieren plan pago; las voces predeterminadas funcionan en el plan gratis (`python scripts/test_elevenlabs.py` lo diagnostica).

> Las cuentas trial de Twilio reproducen un aviso antes de conectar la llamada; con una recarga mínima desaparece.

### 4. Agenda + correo → Google
1. console.cloud.google.com → nuevo proyecto → habilita **Google Calendar API** y **Gmail API**.
2. *OAuth consent screen* → External → agrega tu Gmail como *Test user*.
3. *Credentials* → *OAuth client ID* → **Desktop app** → descarga el JSON en `secrets/google_client_secret.json`.
4. `python scripts/google_auth.py` (abre el navegador una vez) → `CALENDAR_PROVIDER=google`.

### 5. Verificar todo antes de la demo
```bash
python scripts/check_setup.py
```

## Base de contactos (CSV)
Columnas: `nombre, apellidos, empresa, cargo, email, linkedin_url, telefono_consentido, escenario_demo`.
- `telefono_consentido`: número de prueba de alguien que **dio su consentimiento** (tu celular).
- `escenario_demo`: solo para la voz simulada: `acepta | info | referido | no_interesado | no_contesta`.

## Cumplimiento y seguridad
- **Sin scraping de LinkedIn**: solo resultados públicos de buscador vía API.
- **`SANDBOX_MODE=true`** (por defecto): solo se marcan números con consentimiento o en lista blanca; el resto queda `BLOCKED`.
- El agente **se identifica como asistente virtual** y respeta "no me vuelvas a llamar".
- Webhooks autenticados con secreto compartido; los teléfonos se enmascaran en la API y el dashboard.
- Datos personales solo en SQLite local (`data/bot.db`, fuera de git). Ley 1581 de 2012 (Colombia): finalidad, consentimiento y supresión.

## Estructura
```
app/
  main.py            API + webhooks + inyección de proveedores
  pipeline.py        orquestador / máquina de estados / guarda de cumplimiento
  conversation.py    herramientas que el agente usa en la llamada (agenda, correo, referido)
  script.py          guion del Word → prompt del agente + plantilla del correo
  providers/         linkedin.py · enrichment.py · voice.py · calendar.py (real + mock cada uno)
  static/index.html  dashboard
docs/                arquitectura (1 página), guion del video, preparación de la demo
scripts/             google_auth.py · check_setup.py · test_elevenlabs.py
tests/               pytest
```
