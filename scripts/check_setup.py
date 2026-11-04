"""Pre-flight check before the demo: verifies every configured provider.

    python scripts/check_setup.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

s = get_settings()
OK, FAIL, SKIP = "\033[32m✔\033[0m", "\033[31m✘\033[0m", "\033[33m–\033[0m"


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        print(f"PUBLIC_BASE_URL = {s.public_base_url}   SANDBOX = {s.sandbox_mode}")
        if s.public_base_url.startswith("https://") and "xxxx" not in s.public_base_url:
            try:
                r = await c.get(f"{s.public_base_url}/api/status")
                print(OK if r.status_code == 200 else FAIL, "Túnel público responde", r.status_code)
            except Exception as e:
                print(FAIL, "Túnel público no responde (¿ngrok + uvicorn corriendo?)", e)
        else:
            print(SKIP, "Túnel aún no configurado (paso 2)")

        if s.linkedin_provider == "serpapi":
            r = await c.get("https://serpapi.com/account.json", params={"api_key": s.serpapi_api_key})
            j = r.json() if r.status_code == 200 else {}
            print(OK if r.status_code == 200 else FAIL, "SerpAPI", j.get("total_searches_left", r.text[:120]), "búsquedas restantes")
        else:
            print(SKIP, "LinkedIn en mock")

        if "signalhire" in s.enrichment_provider:
            r = await c.get("https://www.signalhire.com/api/v1/credits", headers={"apikey": s.signalhire_api_key})
            print(OK if r.status_code == 200 else FAIL, "SignalHire créditos:", r.text[:120])
        else:
            print(SKIP, "Enriquecimiento en mock")

        if s.voice_provider == "vapi":
            r = await c.get(f"https://api.vapi.ai/phone-number/{s.vapi_phone_number_id}",
                            headers={"Authorization": f"Bearer {s.vapi_api_key}"})
            j = r.json() if r.status_code == 200 else {}
            print(OK if r.status_code == 200 else FAIL, "Vapi número:", j.get("number", r.text[:120]), j.get("provider", ""))
        else:
            print(SKIP, "Voz en mock")

    if s.calendar_provider == "google":
        try:
            from app.providers.calendar import GoogleCalendarService
            g = GoogleCalendarService(s.google_credentials_file, s.google_token_file, s.google_calendar_id, s.tz,
                                      s.meeting_duration_min, s.business_hours_start, s.business_hours_end)
            slots = await g.free_slots(3)
            print(OK, "Google Calendar: próximos horarios libres", [x.strftime("%a %d %H:%M") for x in slots])
        except Exception as e:
            print(FAIL, "Google Calendar:", e)
    else:
        print(SKIP, "Calendario en mock")


asyncio.run(main())
