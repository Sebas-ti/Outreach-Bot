# Guion del video (≤ 5 min)

## Preparación (antes de grabar)
- **Terminal 1:** `ngrok http --url=<tu-dominio>.ngrok-free.dev 8000`. **Terminal 2:** servidor. Luego `python scripts/check_setup.py` → todo ✔.
- **CSV de la demo:** la persona con consentimiento **sin `linkedin_url`** (para que el bot la busque) + 2 contactos ficticios (para mostrar "sin datos → siguiente").
- **Dashboard** en http://localhost:8000 a pantalla completa, con zoom del navegador al 110 %. Celular en **altavoz** cerca del micrófono.
- **Gmail** del contacto y **Google Calendar** abiertos en otras pestañas.
- **Grabación:** OBS o Win+Alt+R a 1080p, capturando el audio del sistema y del micrófono. Ensaya 1 o 2 veces; graba solo si `check_setup.py` está en verde.

## Guion
| Tiempo | Pantalla | Qué decir |
|---|---|---|
| 0:00–0:20 | Dashboard (Pipeline) | "Este bot hace el seguimiento de los contactos de ANDICOM para Lenovo: encuentra su LinkedIn, consigue el celular, lo llama un agente de voz con IA que sigue el guion del cliente, y agenda la reunión con invitación de calendario." |
| 0:20–0:50 | README (diagrama) o chips de proveedores | Stack en 20 s: FastAPI como orquestador, SerpAPI, SignalHire, Vapi + ElevenLabs y Google. "Cada etapa tiene versión real y simulada; aquí todo está en **real** (chips en verde) y en **modo sandbox**: solo se llama a quien dio su consentimiento." |
| 0:50–1:20 | Importar CSV → ▶ Ejecutar | Señala la bitácora: "Encontró el LinkedIn con 100 % de coincidencia, **sin URL**, solo con nombre, empresa y cargo. SignalHire responde por webhook; aquí entra la cascada. Este contacto ficticio no tiene datos y el bot **pasa al siguiente**." |
| 1:20–3:20 | Banner "Llamada en curso" + celular | **Llamada real.** El contacto cuenta su proyecto, lanza una objeción ("no tengo tiempo"), el agente insiste en los 15 minutos, consulta la agenda real, confirma el correo deletreado y agenda. Cuelga solo. |
| 3:20–3:50 | Gmail + Calendar | La invitación llega con link de Meet; la descripción del evento trae el contexto de la llamada para el especialista. |
| 3:50–4:30 | Panel de detalle → Analítica | Reproduce 5 s de la **grabación**, muestra la **transcripción tipo chat** y la **calificación** (proyecto, etapa, fecha, decisores). En **Analítica**: tasa de contacto, de agendamiento y **costo por reunión (~US$ 0,20 por llamada)**. |
| 4:30–4:50 | Pestaña Logs | "Todo queda auditado y se puede exportar." |
| 4:50–5:00 | Cierre | "Siguientes pasos: CRM, reintentos con WhatsApp y concurrencia. El código y la arquitectura están en el repositorio." |

**Tips:** corta los silencios al editar (CapCut / Clipchamp). Si la llamada sale mal, repítela; el plan B es mostrar el flujo con proveedores simulados. No muestres el `.env` ni las API keys en pantalla.
