# Outreach Bot Â· Seguimiento post-ANDICOM

Bot que toma una base de contactos (nombre, apellidos, empresa, cargoâ€¦) y, por cada registro:

1. **Identifica su perfil de LinkedIn** (bÃºsqueda pÃºblica `site:linkedin.com/in` en cascada DuckDuckGo â†’ Google + score de coincidencia).
2. **Enriquece el celular** con SignalHire (cascada de proveedores). Sin datos â†’ **pasa al siguiente**.
3. **Llama** con un agente de voz IA (Vapi + ElevenLabs) que sigue el guion de Lenovo/ANDICOM, califica y maneja objeciones.
   El agente se adapta al contacto: **voz y nombre segÃºn su gÃ©nero** (a un hombre lo llama SofÃ­a, a una mujer Daniel;
   configurable), **habla espaÃ±ol, inglÃ©s o portuguÃ©s** (abre en el idioma de la base, con voz de acento nativo
   para contactos de habla inglesa, y solo cambia de idioma si la persona lo pide: un "hello" no basta)
   y menciona lo que la persona **vio en el stand** si la base lo trae.
4. **Agenda** una reuniÃ³n de 15 min en Google Calendar con link de Meet y **envÃ­a la invitaciÃ³n** al correo,
   o envÃ­a el correo de seguimiento del guion si la persona prefiere informaciÃ³n.

Todo queda auditado en un dashboard en vivo con tres secciones: **Pipeline** (embudo, progreso por contacto, llamada en curso, detalle con grabaciÃ³n y transcripciÃ³n), **AnalÃ­tica** (tasas de contacto y agendamiento, costo por reuniÃ³n, grÃ¡ficas) y **Logs** (filtros y exportaciÃ³n a CSV).

ðŸ“„ Documento de 1 pÃ¡gina: [`docs/Arquitectura_OutreachBot.pdf`](docs/Arquitectura_OutreachBot.pdf) Â· [Word](docs/Arquitectura_OutreachBot.docx) Â· [Markdown](docs/ARQUITECTURA.md)

```
CSV â”€â–º Orquestador (FastAPI + SQLite, mÃ¡quina de estados por contacto)
         â”‚ 1 SerpAPI â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â–º perfil LinkedIn (score â‰¥ 0.6)
         â”‚ 2 SignalHire â—„â”€webhookâ”€ celular (cascada; si no hay â†’ siguiente)
         â”‚ 3 Guarda de cumplimiento (sandbox / consentimiento)
         â”‚ 4 Vapi â”€â”€â”€ llamada â”€â”€â–º persona
         â”‚      â—„â”€â”€ tool-calls â”€â”€ consultar_disponibilidad / agendar_reunion / enviar_resumen_correo / registrar_referido
         â”‚      â—„â”€â”€ end-of-call â”€ transcripciÃ³n + resumen + datos estructurados
         â”” 5 Google Calendar (Meet + invitaciÃ³n)  Â·  Gmail (seguimiento)
```

## Arranque rÃ¡pido (modo simulado, sin cuentas)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate en mac/linux)
pip install -r requirements.txt
copy .env.example .env           # deja todo en mock
uvicorn app.main:app --reload
```
Abre http://localhost:8000 â†’ **Cargar base de ejemplo** â†’ **â–¶ Ejecutar pipeline**.
Tests: `pytest` (30 pruebas: scoring LinkedIn, parsing y emparejamiento de SignalHire, cascadas, guarda de cumplimiento, validaciÃ³n de horarios, webhooks, respaldo por consulta a Vapi, persona por gÃ©nero, idiomas, interrupciones, referidos, reintento por contacto, end-to-end).

## Modo real (paso a paso)

Cada etapa se activa por separado en `.env`, asÃ­ puedes probar una a la vez.

### 0. TÃºnel pÃºblico (para los webhooks de Vapi y SignalHire)
Instala [ngrok](https://ngrok.com/download), luego `ngrok http 8000` y copia la URL https en `PUBLIC_BASE_URL`.
Pon un valor largo y aleatorio en `WEBHOOK_SECRET`.

### 1. LinkedIn â†’ SerpAPI
Crea una cuenta en serpapi.com (plan gratis) â†’ copia la API key â†’ `SERPAPI_API_KEY`, `LINKEDIN_PROVIDER=serpapi`.

### 2. Celular â†’ SignalHire
Crea una cuenta en signalhire.com â†’ *Integrations / API* â†’ copia la API key â†’ `SIGNALHIRE_API_KEY`.
Usa `ENRICHMENT_PROVIDER=signalhire` (o `signalhire,mock` para la cascada en la demo).
SignalHire responde de forma **asÃ­ncrona** en `/webhooks/signalhire`, por eso necesita el tÃºnel.

### 3. Llamada â†’ Vapi (+ nÃºmero Twilio para llamar a Colombia)
1. dashboard.vapi.ai â†’ *API Keys* â†’ **Private key** â†’ `VAPI_API_KEY`.
2. Los nÃºmeros gratis de Vapi solo llaman a EE. UU. Para llamar a un celular colombiano:
   crea una cuenta trial en Twilio â†’ compra un nÃºmero (sale del crÃ©dito de prueba) â†’
   *Verified Caller IDs*: verifica **tu** celular â†’ *Voice Geo Permissions*: habilita **Colombia**.
3. En Vapi â†’ *Phone Numbers* â†’ *Import* â†’ Twilio (Account SID + Auth Token + nÃºmero) â†’ copia el **ID** â†’ `VAPI_PHONE_NUMBER_ID`.
4. `VOICE_PROVIDER=vapi`. El asistente se crea **por llamada** desde el cÃ³digo (`app/providers/voice.py`), no hay que configurarlo en el dashboard.
5. *(Opcional)* Voz por gÃ©nero: `VAPI_VOICE_ID_MALE=<id>` (voz del agente hombre, debe estar en "My Voices" de ElevenLabs), `AGENT_NAME_MALE`, `AGENT_GENDER_MODE=opposite | female | male`. Idiomas: `VAPI_MULTILINGUAL=true` con `VAPI_TRANSCRIBER_MODEL=nova-3` (Deepgram `multi`: espaÃ±ol, inglÃ©s y portuguÃ©s).
6. *(Opcional)* Voz mÃ¡s natural: conecta tu API key de ElevenLabs en Vapi (*Integrations*) y usa `VAPI_VOICE_PROVIDER=11labs`, `VAPI_VOICE_ID=<id>`, `VAPI_VOICE_MODEL=eleven_flash_v2_5`. Las voces de la biblioteca de ElevenLabs requieren plan pago; las voces predeterminadas funcionan en el plan gratis (`python scripts/test_elevenlabs.py` lo diagnostica).

> Las cuentas trial de Twilio reproducen un aviso antes de conectar la llamada; con una recarga mÃ­nima desaparece.

### 4. Agenda + correo â†’ Google
1. console.cloud.google.com â†’ nuevo proyecto â†’ habilita **Google Calendar API** y **Gmail API**.
2. *OAuth consent screen* â†’ External â†’ agrega tu Gmail como *Test user*.
3. *Credentials* â†’ *OAuth client ID* â†’ **Desktop app** â†’ descarga el JSON en `secrets/google_client_secret.json`.
4. `python scripts/google_auth.py` (abre el navegador una vez) â†’ `CALENDAR_PROVIDER=google`.

### 5. Verificar todo antes de la demo
```bash
python scripts/check_setup.py
```

## Base de contactos (CSV o Excel)
Columnas: `nombre, apellidos, empresa, cargo, email, linkedin_url, telefono_consentido, escenario_demo`, y opcionales `idioma, genero, notas_stand`.
- `telefono_consentido`: nÃºmero de prueba de alguien que **dio su consentimiento** (tu celular).
- `escenario_demo`: solo para la voz simulada: `acepta | info | referido | no_interesado | no_contesta`.
- `idioma`: `es | en | pt` (tambiÃ©n "inglÃ©s", "portuguÃ©s"â€¦). Es el idioma de apertura y elige la voz (`VAPI_VOICE_ID_EN`: acento estadounidense). En la llamada solo cambia si la persona lo pide explÃ­citamente.
- `genero`: `F | M` (o "mujer"/"hombre"). Si falta, se infiere del primer nombre (lista local + terminaciÃ³n, sin APIs externas); si no se puede, usa el agente por defecto.
- `notas_stand`: lo que vio o preguntÃ³ en el stand (p. ej. "servidores ThinkSystem para IA"); el agente lo menciona en la apertura, como pide el guion.

Si en la llamada la persona refiere a otra ("no soy la persona indicada"), el referido se **crea como contacto nuevo** (misma empresa, pendiente) para la siguiente corrida; nunca se asume consentimiento sobre su nÃºmero.
En el panel de cada contacto, **Reintentar** vuelve a correr solo ese registro sin tocar el resto del lote.

## Cumplimiento y seguridad
- **Sin scraping de LinkedIn**: solo resultados pÃºblicos de buscador vÃ­a API.
- **`SANDBOX_MODE=true`** (por defecto): solo se marcan nÃºmeros con consentimiento o en lista blanca; el resto queda `BLOCKED`.
- El agente **se identifica como asistente virtual** y respeta "no me vuelvas a llamar".
- Webhooks autenticados con secreto compartido; los telÃ©fonos se enmascaran en la API y el dashboard.
- Datos personales solo en SQLite local (`data/bot.db`, fuera de git). Ley 1581 de 2012 (Colombia): finalidad, consentimiento y supresiÃ³n.

## Estructura
```
app/
  main.py            API + webhooks + inyecciÃ³n de proveedores
  pipeline.py        orquestador / mÃ¡quina de estados / guarda de cumplimiento
  conversation.py    herramientas que el agente usa en la llamada (agenda, correo, referido)
  script.py          guion del Word â†’ prompt del agente + plantilla del correo
  providers/         linkedin.py Â· enrichment.py Â· voice.py Â· calendar.py (real + mock cada uno)
  static/index.html  dashboard
docs/                arquitectura (1 pÃ¡gina: Markdown, PDF y Word)
scripts/             google_auth.py Â· check_setup.py Â· test_elevenlabs.py
tests/               pytest
```
