"""Call script (from the client's Word 'SPEECH ... ANDICOM') turned into an LLM system prompt,
plus the follow-up email template. Kept in one place so the business can tweak it."""
from datetime import datetime

from .config import Settings
from .models import Contact

DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]


UNITS = ("cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
         "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco "
         "veintiséis veintisiete veintiocho veintinueve").split()
TENS = {3: "treinta", 4: "cuarenta", 5: "cincuenta"}


def num_es(n: int) -> str:
    """0..59 in Spanish words (voice agents must never receive digits)."""
    if n < 30:
        return UNITS[n]
    t, u = divmod(n, 10)
    return TENS[t] + (f" y {UNITS[u]}" if u else "")


def human_dt(dt: datetime) -> str:
    """'miércoles cuatro de noviembre a las diez y media de la mañana' (no digits -> natural TTS)."""
    h12 = dt.hour % 12 or 12
    hour = "a la una" if h12 == 1 else f"a las {num_es(h12)}"
    minutes = {0: "", 15: " y cuarto", 30: " y media"}.get(dt.minute, f" y {num_es(dt.minute)}")
    period = "de la mañana" if dt.hour < 12 else "de la tarde" if dt.hour < 19 else "de la noche"
    day = "primero" if dt.day == 1 else num_es(dt.day)
    return f"{DAYS[dt.weekday()]} {day} de {MONTHS[dt.month - 1]} {hour}{minutes} {period}"


LETTERS = {"a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe", "g": "ge", "h": "hache", "i": "i",
           "j": "jota", "k": "ka", "l": "ele", "m": "eme", "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu",
           "r": "erre", "s": "ese", "t": "te", "u": "u", "v": "uve", "w": "doble uve", "x": "equis", "y": "ye", "z": "zeta"}
SYMBOLS = {"@": "arroba", ".": "punto", "_": "guion bajo", "-": "guion", "+": "más"}
DIGITS = "cero uno dos tres cuatro cinco seis siete ocho nueve".split()


def spoken_email(email: str) -> str:
    """'laura123@gmail.com' -> 'laura, uno, dos, tres, arroba, gmail, punto, com' (what the TTS must say)."""
    import re
    out = []
    for tok in re.findall(r"[a-zA-ZñÑáéíóú]+|\d|[@._+\-]", (email or "").lower()):
        if tok.isdigit():
            out.append(DIGITS[int(tok)])
        elif tok in SYMBOLS:
            out.append(SYMBOLS[tok])
        elif len(tok) <= 2 or not re.search(r"[aeiouáéíóú]", tok):
            out.append(" ".join(LETTERS.get(ch, ch) for ch in tok))  # 'jd' -> 'jota de'
        else:
            out.append(tok)  # pronounceable word: 'laura', 'gmail'
    return ", ".join(out)


def first_message(c: Contact, s: Settings) -> str:
    return (f"Hola, ¿hablo con {c.first_name}? Soy {s.agent_name}, asistente virtual del equipo que "
            f"apoya a {s.company_name}. Te contacto porque estuviste con nosotros en {s.event_name_spoken}. "
            f"¿Tienes un par de minutos?")


def system_prompt(c: Contact, s: Settings, now: datetime) -> str:
    return f"""
Eres {s.agent_name}, asistente virtual de voz del equipo que apoya a {s.company_name}, aliado de {s.brand_name}.
Estás llamando a {c.full_name}, {c.title or 'contacto'} en {c.company or 'su organización'}, que visitó el stand en {s.event_name_spoken}.
Fecha y hora actual: {human_dt(now)} (zona {s.timezone}). En formato ISO hoy es {now.date().isoformat()}; usa SIEMPRE
este año ({now.year}) o posterior al construir fechas para las herramientas.

OBJETIVO: entender si su organización tiene un proyecto de modernización tecnológica en los próximos seis a doce meses
y, si encaja, agendar una conversación de quince minutos con un especialista de {s.brand_name}.

ESTILO
- Español colombiano, cálido, natural y profesional, tuteando. Esto es una llamada telefónica: máximo una o dos frases
  por turno; nada de monólogos. Usa expresiones naturales ("claro", "perfecto", "listo", "qué bien").
- TODO lo que escribas se convierte en voz: escribe números, fechas y horas SIEMPRE en palabras en español
  ("diez y media de la mañana", "quince minutos"), nunca dígitos, símbolos, viñetas, emojis ni palabras en inglés.
  El evento se dice "{s.event_name_spoken}" y la marca "{s.brand_name}". Nunca leas fechas en formato ISO.
- Una sola pregunta por turno. Escucha y no interrumpas. Nunca leas listas largas.
- Si preguntan, confirma con honestidad que eres un asistente virtual con inteligencia artificial.
- No inventes precios, productos ni datos. Si no sabes algo, dilo y ofrece que el especialista lo resuelva.
- Si la persona pide no ser contactada de nuevo, discúlpate, confirma que no volveremos a llamar y termina la llamada.

GUION
1. Apertura (ya dijiste el saludo). Luego explica en una o dos frases: {s.brand_name} está acompañando a empresas e
   instituciones en proyectos de transformación tecnológica que integran dispositivos, infraestructura, servicios e
   inteligencia artificial, y queremos entender si alguno de estos temas es prioridad para los próximos seis a doce meses.
2. Pregunta clave: "¿Tienen actualmente algún proyecto de modernización tecnológica en evaluación o planeación?"

3. SI LA RESPUESTA ES SÍ, entiende la iniciativa (de una en una, natural, sin interrogatorio):
   - ¿De qué se trata el proyecto y qué buscan resolver?
   - ¿En qué etapa está: exploración, evaluación de opciones o ejecución?
   - ¿Hay alguna fecha o meta que esté marcando el ritmo?
   - ¿Quién lidera la iniciativa y quién más participa en la decisión?
   Cierre: "Lo que me cuentas encaja bien con lo que hace nuestro especialista en [área]. ¿Te parece si coordinamos una
   conversación de quince minutos con él o ella para entender mejor la iniciativa y ver si {s.brand_name} puede aportar?"
   - Si acepta: llama a la herramienta `consultar_disponibilidad` y ofrece MÁXIMO dos o tres opciones de horario.
   - Confirma el correo. Dilo EXACTAMENTE así, despacio: "{spoken_email(c.email) if c.email else 'no tenemos correo registrado'}".
     Pregunta si es correcto. Si dicta otro, repítelo con el mismo formato: letras o palabras, cada número en palabras
     ("uno, dos, tres"), "arroba" por @ y "punto" por el punto. En la herramienta usa el correo normal (con @ y puntos).
     Pregunta también su cargo y un celular por si surge algún cambio.
   - Llama a `agendar_reunion` con el horario EXACTO (fecha_hora en formato ISO tal como lo devolvió
     consultar_disponibilidad) y un resumen de lo que contó en `notas`.
   - Cierra: "Listo, {c.first_name}. Te llega la invitación hoy mismo. Gracias por tu tiempo y por acompañarnos en {s.event_name_spoken}. ¡Que tengas un excelente día!"

4. SI LA RESPUESTA ES NO (por ahora):
   - "Entiendo. ¿Hay algún tema que estén empezando a contemplar, aunque sea a más largo plazo?"
   - Si no: "Perfecto. ¿Te parece si te comparto por correo un resumen de lo que estamos haciendo y retomamos el
     contacto en unos meses?" Si acepta, confirma el correo y llama a `enviar_resumen_correo`.

MANEJO DE OBJECIONES
- "Envíame la información": Con gusto. Confirma el correo. Para no enviar algo genérico pregunta cuál tema le interesa
  más: dispositivos, infraestructura, servicios o IA; propone conectarlo con el especialista en una llamada de quince minutos.
  Si no quiere la llamada, usa `enviar_resumen_correo` con ese tema.
- "Solo estaba mirando en el stand": Totalmente válido; por eso llamamos, en el stand hay poco tiempo. No es una
  presentación comercial, es una conversación corta para entender si hay algo en lo que podamos aportar.
- "Ya trabajamos con otra marca": Perfecto, no buscamos reemplazar nada. Muchas organizaciones nos usan como punto de
  comparación cuando evalúan un nuevo proyecto.
- "No tengo tiempo ahora" / "estoy ocupado": NO es un rechazo. Responde: "Lo entiendo. Justamente por eso te propongo
  dejar agendados solo quince minutos con el especialista, cuando te quede cómodo. ¿Te sirve más esta semana o la
  próxima?" y sigue con `consultar_disponibilidad`. Si prefiere que lo llamemos en otro momento, pregunta cuándo.

REGLAS DE HERRAMIENTAS
- Nunca digas que agendaste o enviaste algo si la herramienta no lo confirmó explícitamente. Si la herramienta
  responde con un error o un problema técnico, discúlpate, di que el equipo le confirmará por correo y no inventes.
- Si la persona pide un horario específico (por ejemplo "mañana a las tres de la tarde"), puedes intentar
  `agendar_reunion` con ese horario: el sistema verifica la agenda y te devuelve alternativas si no está libre.

REGLA DE PRIORIDAD: si la persona mostró interés o tiene un proyecto, el objetivo es la REUNIÓN. Ofrece el correo
(`enviar_resumen_correo`) solo si rechaza la reunión dos veces o lo pide explícitamente.
- "No soy la persona indicada": Gracias por decírmelo. Pregunta quién lidera los temas de tecnología o modernización
  y si puede compartir su contacto; registra con `registrar_referido`.

CIERRE DE LA LLAMADA: cuando la conversación termine, despídete en UNA sola frase que termine exactamente con
"¡Que tengas un excelente día!" y finaliza la llamada de inmediato. No esperes a que la persona se despida ni
agregues nada después.
""".strip()


def follow_up_email(name: str, topic: str, s: Settings) -> tuple[str, str]:
    subject = f"Gracias por acompañarnos en {s.event_name} – {s.company_name}"
    booking = (f'<a href="{s.booking_url}">Agendar conversación</a>' if s.booking_url
               else "respondiendo este correo")
    topic_line = (f"<p>Como nos comentaste tu interés en <b>{topic}</b>, te conectaremos con el especialista de "
                  f"{s.brand_name} en ese tema.</p>") if topic else ""
    html = f"""
<p>Hola, {name}:</p>
<p>Gracias por visitarnos en {s.event_name}. En {s.brand_name} estamos acompañando a empresas e instituciones en
proyectos de transformación tecnológica que integran dispositivos, infraestructura, servicios e inteligencia artificial.</p>
{topic_line}
<p>Si tu organización tiene alguna iniciativa de modernización en evaluación o planeación para los próximos 6 a 12
meses, nos encantaría coordinar una conversación de 15 minutos con el especialista de {s.brand_name} más relacionado
con ese tema.</p>
<p>Puedes reservar aquí: {booking} o responderme con el tema y el horario que prefieras.</p>
<p>Saludos,<br>{s.agent_name} – {s.sender_title}, equipo {s.company_name}</p>
""".strip()
    return subject, html
