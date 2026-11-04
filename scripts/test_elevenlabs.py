"""Diagnose an ElevenLabs voice directly (without placing a call).

Add to .env:   ELEVENLABS_API_KEY=sk_...
Run:           python scripts/test_elevenlabs.py
Writes data/voz_prueba.mp3 if the voice works; otherwise prints ElevenLabs' exact error.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402

from app.config import get_settings  # noqa: E402

s = get_settings()
key = os.getenv("ELEVENLABS_API_KEY") or dotenv_values(".env").get("ELEVENLABS_API_KEY", "")
if not key:
    sys.exit("Falta ELEVENLABS_API_KEY en .env")
H = {"xi-api-key": key}
voice = s.vapi_voice_id

with httpx.Client(timeout=30) as c:
    u = c.get("https://api.elevenlabs.io/v1/user/subscription", headers=H)
    print("Cuenta:", u.status_code, u.json().get("tier") if u.status_code == 200 else u.text[:200])
    if u.status_code == 200:
        j = u.json()
        print(f"  caracteres usados {j.get('character_count')} / {j.get('character_limit')}")

    v = c.get(f"https://api.elevenlabs.io/v1/voices/{voice}", headers=H)
    print("Voz en tu cuenta:", v.status_code, (v.json().get("name"), v.json().get("category")) if v.status_code == 200 else v.text[:300])

    t = c.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", headers=H, json={
        "text": "Hola, ¿hablo con Juan Sebastián? Te contacto porque estuviste con nosotros en Andicom.",
        "model_id": s.vapi_voice_model or "eleven_flash_v2_5", "language_code": "es"})
    if t.status_code == 200:
        Path("data/voz_prueba.mp3").write_bytes(t.content)
        print("✔ TTS OK → data/voz_prueba.mp3 (ábrelo para escuchar la voz)")
    else:
        print("✘ TTS falló:", t.status_code, t.text[:500])
