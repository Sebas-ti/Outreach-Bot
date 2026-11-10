from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    public_base_url: str = "http://localhost:8000"
    webhook_secret: str = "dev-secret"
    database_path: str = "data/bot.db"
    timezone: str = "America/Bogota"

    # mock | real provider per stage
    linkedin_provider: str = "mock"  # mock | serpapi
    enrichment_provider: str = "mock"  # mock | signalhire
    voice_provider: str = "mock"  # mock | vapi
    calendar_provider: str = "mock"  # mock | google

    # 1. LinkedIn
    serpapi_api_key: str = ""
    linkedin_min_confidence: float = 0.6

    # 2. Enrichment
    signalhire_api_key: str = ""
    enrichment_timeout_s: int = 90

    # 3. Voice
    vapi_api_key: str = ""
    vapi_phone_number_id: str = ""
    vapi_model_provider: str = "openai"
    vapi_model: str = "gpt-4o-mini"
    vapi_voice_provider: str = "azure"
    vapi_voice_id: str = "es-CO-SalomeNeural"
    vapi_voice_model: str = ""  # e.g. eleven_flash_v2_5 (11labs) / sonic-2 (cartesia)
    vapi_voice_id_male: str = ""  # voice for the male persona (empty -> same voice as VAPI_VOICE_ID)
    # Native-accent voices when the base already says the contact speaks that language (empty -> Spanish voice)
    vapi_voice_id_en: str = ""  # e.g. ElevenLabs "Bella" (American, female)
    vapi_voice_id_en_male: str = ""  # e.g. ElevenLabs "Eric" (American, male)
    vapi_voice_id_pt: str = ""
    vapi_voice_id_pt_male: str = ""
    vapi_voice_id_fr: str = ""  # e.g. ElevenLabs "Lucie" (French, female)
    vapi_voice_id_fr_male: str = ""  # e.g. ElevenLabs "Marcel" (French, male)
    # Barge-in: words the person must say before the agent stops (0 = any voice activity, too sensitive to
    # people talking nearby). Interruption phrases ("espera", "wait") still stop it instantly.
    vapi_stop_num_words: int = 2
    # Prank/abusive calls: after the agent flags it, the server force-ends the call if it is still up
    abuse_hangup_after_s: float = 12
    vapi_transcriber_model: str = "nova-3"  # nova-3 + multi = Spanish/English/Portuguese in one call
    vapi_multilingual: bool = True  # the agent follows the language the person speaks (es/en/pt)
    vapi_wait_seconds: float = 0.3
    # assistant-waits-for-user: like a human caller, let the person say "¿Aló?" first (instead of talking the
    # instant they pick up). assistant-speaks-first restores the previous behaviour.
    vapi_first_message_mode: str = "assistant-waits-for-user"
    # If nobody says anything after picking up, the server makes the agent say "¿Aló?" (Vapi's idle messages
    # only start after the first exchange). Seconds of silence before each nudge; 0 disables it.
    silence_nudge_after_s: float = 5
    silence_nudge_max: int = 2
    vapi_temperature: float = 0.7  # some variety in wording so it doesn't sound recited
    # ElevenLabs: lower stability = more expressive, less monotone delivery
    vapi_voice_stability: float = 0.4
    vapi_voice_similarity: float = 0.8
    call_timeout_s: int = 600
    mock_call_delay_s: float = 1.5

    # 4. Calendar / email
    google_credentials_file: str = "secrets/google_client_secret.json"
    google_token_file: str = "secrets/google_token.json"
    google_calendar_id: str = "primary"
    meeting_duration_min: int = 15
    business_hours_start: int = 9
    business_hours_end: int = 17
    booking_url: str = ""

    # Script personalization
    agent_name: str = "Sofía"  # female persona
    agent_name_male: str = "Daniel"  # male persona
    agent_gender_mode: str = "opposite"  # opposite (man->female agent, woman->male agent) | female | male
    agent_default_gender: str = "f"  # when the contact's gender can't be determined
    default_language: str = "es"  # opening language when the CSV has no 'idioma' column (es | en | pt)
    company_name: str = "Empresa X"
    brand_name: str = "Lenovo"
    event_name: str = "ANDICOM 2026"  # written (emails, calendar)
    event_name_spoken: str = "Andicom"  # how the voice agent says it
    sender_title: str = "Ejecutiva de cuenta"
    sender_title_male: str = "Ejecutivo de cuenta"
    sender_phone: str = ""  # signature of the follow-up email (Word: [Teléfono] · [Correo])
    sender_email: str = ""

    # Compliance
    sandbox_mode: bool = True
    allowed_phone_numbers: str = ""
    default_country_code: str = "57"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def allowed_numbers(self) -> set[str]:
        return {n.strip() for n in self.allowed_phone_numbers.split(",") if n.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
