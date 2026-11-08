"""Who speaks on the call and in which language.

- Agent gender: by default the OPPOSITE of the contact's (configurable), each with its own name and voice.
- Contact gender: CSV column `genero` if present; otherwise inferred from the first name (offline list +
  suffix heuristic; no external API, so no personal data leaves the system). Unknown -> default agent.
- Language: CSV column `idioma` (es | en | pt) sets the opening; mid-call the agent follows whatever
  language the person speaks (multilingual transcriber + prompt rule).
"""
from dataclasses import dataclass

from .config import Settings
from .models import Contact
from .utils import normalize_text

LANGUAGES = {"es": "español", "en": "inglés", "pt": "portugués"}
_LANG_ALIASES = {
    "es": "es", "esp": "es", "espanol": "es", "spanish": "es", "castellano": "es",
    "en": "en", "eng": "en", "ingles": "en", "english": "en",
    "pt": "pt", "por": "pt", "portugues": "pt", "portuguese": "pt", "ptbr": "pt", "br": "pt", "brasil": "pt",
}
_GENDER_ALIASES = {
    "f": "f", "fem": "f", "femenino": "f", "mujer": "f", "female": "f", "woman": "f", "feminino": "f", "mulher": "f",
    "m": "m", "masc": "m", "masculino": "m", "hombre": "m", "male": "m", "man": "m", "homem": "m", "h": "m",
}

FEMALE_NAMES = set("""
isabel carmen raquel beatriz ines mercedes dolores luz ruth ester esther pilar rocio consuelo soledad nieves belen
marisol gisel giselle jennifer jenifer karen lizeth lisbeth lizbeth yuliet ingrid astrid miriam noemi abigail maribel
nicole michelle elizabeth ivonne edith judith mabel rosmery shirley leidy leydi yamile marlen nelly carol sharon
madeleine alison allison emily ashley mary sarah sara kimberly jessica tiffany lucy dayana diana yeimy yenny jenny
ana maria rachel hannah megan lauren karol yolanda flor iris abril sol cristal estefany stephany estefani stefany
mariangel maryuri marilyn evelyn jacqueline yaneth janeth lisseth ruby joan wendy cindy daisy betty""".split())
MALE_NAMES = set("""
juan andres luis carlos daniel david sebastian felipe jose jorge jaime dante jesus angel oscar cesar victor hector
manuel miguel rafael gabriel ismael samuel joel noel abel adrian alan alex alexis axel brian bryan christian cristian
dylan edwin elkin esteban ivan jhon john jonathan kevin leonel marlon nelson omar ruben santiago tomas william wilson
yeison henry jeremy timothy anthony tony rodney luca joshua nicola jordan mathias matias nicolas lucas thomas
fernando hernan german julian fabian simon ramon leon elias isaac josue saul raul uriel ariel emanuel""".split())


def normalize_language(value: str) -> str:
    return _LANG_ALIASES.get(normalize_text(value or "").replace(" ", ""), "")


def normalize_gender(value: str) -> str:
    return _GENDER_ALIASES.get(normalize_text(value or "").replace(" ", ""), "")


def infer_gender(first_name: str) -> str:
    """'f' | 'm' | '' (unknown). Uses the first given name: 'María José' -> f, 'José María' -> m."""
    parts = normalize_text(first_name or "").split()
    if not parts:
        return ""
    name = parts[0]
    if name in FEMALE_NAMES:
        return "f"
    if name in MALE_NAMES:
        return "m"
    if name.endswith("a"):
        return "f"
    if name.endswith("o"):
        return "m"
    return ""


def contact_gender(c: Contact) -> str:
    return normalize_gender(c.gender) or infer_gender(c.first_name)


@dataclass(frozen=True)
class Persona:
    name: str
    gender: str  # 'f' | 'm' (the agent's)
    voice_id: str
    language: str  # opening language: es | en | pt
    contact_gender: str  # 'f' | 'm' | ''

    @property
    def is_female(self) -> bool:
        return self.gender == "f"


def agent_persona(c: Contact, s: Settings) -> Persona:
    cg = contact_gender(c)
    mode = (s.agent_gender_mode or "opposite").lower()
    if mode in ("female", "f", "mujer"):
        gender = "f"
    elif mode in ("male", "m", "hombre"):
        gender = "m"
    else:  # opposite: man -> female agent, woman -> male agent, unknown -> default
        gender = {"m": "f", "f": "m"}.get(cg, normalize_gender(s.agent_default_gender) or "f")
    language = normalize_language(c.language) or normalize_language(s.default_language) or "es"
    if gender == "m":
        name, voice = s.agent_name_male, s.vapi_voice_id_male or s.vapi_voice_id
    else:
        name, voice = s.agent_name, s.vapi_voice_id
    # A contact known to speak English/Portuguese gets a native-accent voice (a Colombian voice speaking
    # English sounds strongly accented). Mid-call switches keep the opening voice.
    native = {("en", "f"): s.vapi_voice_id_en, ("en", "m"): s.vapi_voice_id_en_male,
              ("pt", "f"): s.vapi_voice_id_pt, ("pt", "m"): s.vapi_voice_id_pt_male}.get((language, gender))
    return Persona(name=name, gender=gender, voice_id=native or voice, language=language, contact_gender=cg)


# ------------------------------------------------------------------ localized fixed phrases
# Everything the platform speaks WITHOUT going through the LLM must exist in each language.
CLOSING = {"es": "¡Que tengas un excelente día!", "en": "Have a great day!", "pt": "Tenha um ótimo dia!"}
END_CALL_PHRASES = ["que tengas un excelente día", "que tengas un excelente dia", "have a great day",
                    "tenha um ótimo dia", "tenha um otimo dia"]

WAIT_MESSAGES = {
    "consultar_disponibilidad": {"es": "Un momento, reviso la agenda del especialista.",
                                 "en": "One moment, let me check the specialist's calendar.",
                                 "pt": "Um momento, vou verificar a agenda do especialista."},
    "agendar_reunion": {"es": "Perfecto, dame un segundo mientras la agendo.",
                        "en": "Perfect, give me a second while I book it.",
                        "pt": "Perfeito, me dá um segundo enquanto eu agendo."},
    "enviar_resumen_correo": {"es": "Listo, dame un segundo.", "en": "Sure, one second.", "pt": "Claro, um segundo."},
}

IDLE_MESSAGES = {
    "es": ["¿Sigues ahí?", "¿Me escuchas bien?", "Perdón, creo que no te escuché, ¿me repites?"],
    "en": ["Are you still there?", "Can you hear me okay?", "Sorry, I didn't catch that, could you repeat it?"],
    "pt": ["Você ainda está aí?", "Está me ouvindo bem?", "Desculpe, não ouvi, pode repetir?"],
}

# Backchannel words that must NOT cut the agent off, and words that always should.
# Kept minimal on purpose: "claro", "ok" or "sí" are often real answers and must stop the agent.
ACKNOWLEDGEMENT_PHRASES = ["ajá", "aja", "mjm", "mm-hmm", "uh-huh", "uhum"]
INTERRUPTION_PHRASES = ["espera", "espere", "un momento", "perdón", "perdona", "disculpa", "soy yo",
                        "wait", "hold on", "sorry", "espera aí", "desculpa"]


def first_message(c: Contact, s: Settings, p: Persona) -> str:
    if p.language == "en":
        return (f"Hi, am I speaking with {c.first_name}? This is {p.name}, a virtual assistant with the team that "
                f"supports {s.company_name}. I'm calling because you visited us at {s.event_name_spoken}. "
                f"Do you have a couple of minutes?")
    if p.language == "pt":
        return (f"Olá, falo com {c.first_name}? Aqui é {p.name}, assistente virtual da equipe que apoia a "
                f"{s.company_name}. Estou ligando porque você esteve com a gente no {s.event_name_spoken}. "
                f"Você tem um minutinho?")
    return (f"Hola, ¿hablo con {c.first_name}? Soy {p.name}, asistente virtual del equipo que "
            f"apoya a {s.company_name}. Te contacto porque estuviste con nosotros en {s.event_name_spoken}. "
            f"¿Tienes un par de minutos?")


def voicemail_message(c: Contact, s: Settings, p: Persona) -> str:
    return {
        "en": f"Hi {c.first_name}, this is {p.name} from the {s.company_name} team. We'll follow up by email "
              f"about what you saw at {s.event_name_spoken}. Thank you!",
        "pt": f"Olá {c.first_name}, aqui é {p.name} da equipe {s.company_name}. Vamos te escrever por e-mail "
              f"sobre o que você viu no {s.event_name_spoken}. Obrigado!",
    }.get(p.language, f"Hola {c.first_name}, te habla {p.name} del equipo de {s.company_name}. "
                      f"Te escribiremos por correo sobre lo que viste en {s.event_name_spoken}. ¡Gracias!")
