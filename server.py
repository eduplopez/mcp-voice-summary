"""
Servidor MCP de resumen por voz.

Expone una herramienta que lee en voz alta un resumen breve de la accion
recien realizada. Soporta dos motores de sintesis, seleccionables por
variables de entorno:

    VOICE_ENGINE = "sapi5" (por defecto) | "edge"
    VOICE_NAME   = nombre o id de la voz (ver listar_voces)
    VOICE_RATE   = velocidad, 100 es normal, o relativa ("+10%", "-15%")
    VOICE_VOLUME = volumen, 100 es normal

    MAX_SUMMARY_WORDS = tope de seguridad de palabras por resumen (400)

La longitud del resumen no la fija el servidor: la decide el asistente segun
cuanto trabajo haya realizado. MAX_SUMMARY_WORDS solo evita locuciones
accidentales de varios minutos.

Motores:
- sapi5: pyttsx3 sobre el sintetizador nativo del sistema. Offline y gratuito.
  En Windows usa SAPI5; en Linux, NSSpeech; en macOS, NSSS.
- edge:  edge-tts (voces neuronales de Azure). Calidad muy superior, pero
  requiere conexion a internet en cada locucion.

Notas de implementacion:
- mcp 2.x: FastMCP fue renombrado a MCPServer (mcp.server.mcpserver).
- El motor se inicializa una sola vez y se usa desde un unico hilo
  trabajador. pyttsx3 no es thread-safe y COM es apartment-threaded: crear el
  engine en un hilo y usarlo en otro bloquea el proceso indefinidamente.
- Las peticiones se encolan para que dos resumenes consecutivos no se pisen.
- El hilo trabajador es daemon para no bloquear el cierre del proceso.
- Los imports de pyttsx3 y edge_tts son perezosos, para que el servidor
  arranque en cualquier plataforma aunque solo se use uno de los motores.
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("VoiceSummaryServer")

logger = logging.getLogger("voice-summary")

ENGINE = os.environ.get("VOICE_ENGINE", "sapi5").strip().lower()
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()
VOICE_RATE = os.environ.get("VOICE_RATE", "100").strip()
VOICE_VOLUME = os.environ.get("VOICE_VOLUME", "100").strip()

# Reproductor de MP3 forzado por el usuario. Si se vacia, se autodetecta.
VOICE_PLAYER = os.environ.get("VOICE_PLAYER", "").strip()

# Idioma de la voz. Valores:
#   ""        autodeteccion desde el idioma del sistema (recomendado)
#   "es"      forzar un idioma por codigo ISO 639-1
#   "pt-BR"   forzar un idioma y variante regional concreta
#   "auto"    detectar el idioma a partir del texto de cada resumen
# Cualquiera de los dos puede sobrescribirse en caliente con la herramienta
# establecer_idioma.
VOICE_LANGUAGE = os.environ.get("VOICE_LANGUAGE", "").strip()

# Genero de la voz. Valores: "" (el que tenga asignado el idioma), "female" o
# "male". Acepta tambien "f"/"m", "femenina"/"masculina" y "mujer"/"hombre".
# Solo aplica al motor edge: SAPI5 no expone el genero de sus voces.
VOICE_GENDER = os.environ.get("VOICE_GENDER", "").strip().lower()

# Tope de seguridad de palabras por resumen. No es una recomendacion de
# longitud: el resumen lo decide el asistente segun la cantidad de trabajo que
# haya hecho. Este limite solo evita locuciones accidentales de varios minutos
# por un `texto` desmedido. Por defecto 400 palabras, unos 2-3 minutos.
MAX_SUMMARY_WORDS = os.environ.get("MAX_SUMMARY_WORDS", "400").strip()

# Idioma usado cuando no se puede determinar otro. El ingles es el idioma con
# mas cobertura de voces en todos los motores.
DEFAULT_LANGUAGE = "en"

# Voz por defecto segun motor.
DEFAULT_SAPI5_VOICE = "es-es"  # se busca por fragmento de id o nombre
DEFAULT_EDGE_VOICE = "es-ES-AlvaroNeural"

# Idioma forzado en caliente mediante la herramienta establecer_idioma.
# None significa "no forzado": se aplica la precedencia normal.
# El valor "auto" delega en la deteccion sobre el texto de cada resumen.
_idioma_forzado: str | None = None

# Catalogo de voces de edge-tts, cacheado tras la primera consulta.
_catalogo_edge: dict[str, dict] = {}
_catalogo_cargado = False

# Voz que tiene activa el motor sapi5 en este momento.
_voz_sapi5_actual = None  # type: ignore[assignment]

# Peticiones de voz pendientes. El hilo trabajador las consume en orden.
_cola: queue.Queue[str | None] = queue.Queue()

# Estado del motor, inicializado de forma perezosa desde el hilo trabajador.
_lock = threading.Lock()
_sapi5_engine = None  # type: ignore[assignment]


def _normalizar_porcentaje(valor: str, defecto: int = 0) -> str:
    """Convierte un valor de volumen o velocidad al formato que exige edge-tts.

    edge-tts solo admite el formato relativo "+10%" o "-10%". SAPI5 en cambio
    usa una escala absoluta donde 100 es el valor normal. Aceptamos ambas
    notaciones para que la misma variable sirva segun el motor: un numero
    desnudo se interpreta como escala absoluta (100 es normal) y se traduce a
    la variacion relativa equivalente.
    """
    texto = valor.strip()
    if texto.endswith("%"):
        return texto
    try:
        absoluto = int(float(texto))
    except ValueError:
        return f"{defecto:+d}%"
    return f"{absoluto - 100:+d}%"


# --------------------------------------------------------------------------
# Reproduccion de MP3 multiplataforma
# --------------------------------------------------------------------------
def _reproducir_con_mci(ruta: str) -> None:
    """Reproduce un MP3 con el reproductor MCI de Windows."""
    import ctypes

    def mci(comando: str) -> None:
        ctypes.windll.winmm.mciSendStringW(ctypes.c_wchar_p(comando), None, 0, 0)

    mci(f'open "{ruta}" type mpegvideo alias resumen')
    try:
        mci("play resumen wait")
    finally:
        mci("close resumen")


# Reproductores externos, en orden de preferencia, por plataforma.
# Cada entrada es una lista de argumentos; el primer ejecutable disponible gana.
_JUGADORES_EXTERNOS: dict[str, list[list[str]]] = {
    "darwin": [["afplay", "{ruta}"]],
    "linux": [
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{ruta}"],
        ["mpg123", "-q", "{ruta}"],
        ["cvlc", "--play-and-exit", "--quiet", "{ruta}"],
        ["paplay", "{ruta}"],
    ],
}


def _reproducir_con_externo(ruta: str) -> None:
    """Reproduce un MP3 con un reproductor de linea de comandos del sistema."""
    import sys

    candidatos: list[list[str]] = []
    if VOICE_PLAYER:
        candidatos.append([VOICE_PLAYER, "{ruta}"])
    candidatos.extend(_JUGADORES_EXTERNOS.get(sys.platform, []))

    for plantilla in candidatos:
        ejecutable = shutil.which(plantilla[0])
        if not ejecutable:
            continue
        comando = [ruta if p == "{ruta}" else p for p in plantilla]
        comando[0] = ejecutable
        subprocess.run(comando, check=True)
        return

    raise RuntimeError(
        "No se encontro ningun reproductor de audio. Instala uno de: "
        + ", ".join(p[0] for p in candidatos)
        + " o define VOICE_PLAYER."
    )


def _reproducir_mp3(ruta: str) -> None:
    """Reproduce un MP3 con el reproductor disponible en esta plataforma."""
    import sys

    if sys.platform == "win32" and not VOICE_PLAYER:
        _reproducir_con_mci(ruta)
        return
    _reproducir_con_externo(ruta)


# --------------------------------------------------------------------------
# Seleccion de idioma y de voz
# --------------------------------------------------------------------------
# Voz neuronal preferida por idioma y genero: (femenina, masculina). Los
# nombres se han verificado contra el catalogo real de edge-tts; si alguno
# dejara de existir, la busqueda cae automaticamente a cualquier otra voz del
# mismo idioma y genero.
VOZES_POR_IDIOMA: dict[str, tuple[str, str]] = {
    "es": ("es-ES-XimenaNeural", "es-ES-AlvaroNeural"),
    "en": ("en-US-AvaNeural", "en-US-AndrewNeural"),
    "fr": ("fr-FR-VivienneMultilingualNeural", "fr-FR-RemyMultilingualNeural"),
    "de": ("de-DE-SeraphinaMultilingualNeural", "de-DE-FlorianMultilingualNeural"),
    "it": ("it-IT-ElsaNeural", "it-IT-GiuseppeNeural"),
    "pt": ("pt-BR-ThalitaMultilingualNeural", "pt-BR-AntonioNeural"),
    "ca": ("ca-ES-JoanaNeural", "ca-ES-EnricNeural"),
    "gl": ("gl-ES-SabelaNeural", "gl-ES-RoiNeural"),
    "ru": ("ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"),
    "uk": ("uk-UA-PolinaNeural", "uk-UA-OstapNeural"),
    "pl": ("pl-PL-ZofiaNeural", "pl-PL-MarekNeural"),
    "cs": ("cs-CZ-VlastaNeural", "cs-CZ-AntoninNeural"),
    "sk": ("sk-SK-ViktoriaNeural", "sk-SK-LukasNeural"),
    "hu": ("hu-HU-NoemiNeural", "hu-HU-TamasNeural"),
    "ro": ("ro-RO-AlinaNeural", "ro-RO-EmilNeural"),
    "bg": ("bg-BG-KalinaNeural", "bg-BG-BorislavNeural"),
    "el": ("el-GR-AthinaNeural", "el-GR-NestorasNeural"),
    "sv": ("sv-SE-SofieNeural", "sv-SE-MattiasNeural"),
    "da": ("da-DK-ChristelNeural", "da-DK-JeppeNeural"),
    "nb": ("nb-NO-PernilleNeural", "nb-NO-FinnNeural"),
    "fi": ("fi-FI-NooraNeural", "fi-FI-HarriNeural"),
    "nl": ("nl-NL-ColetteNeural", "nl-NL-MaartenNeural"),
    "tr": ("tr-TR-EmelNeural", "tr-TR-AhmetNeural"),
    "ar": ("ar-EG-SalmaNeural", "ar-EG-ShakirNeural"),
    "he": ("he-IL-HilaNeural", "he-IL-AvriNeural"),
    "hi": ("hi-IN-SwaraNeural", "hi-IN-MadhurNeural"),
    "ja": ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural"),
    "ko": ("ko-KR-SunHiNeural", "ko-KR-HyunsuMultilingualNeural"),
    "zh": ("zh-CN-XiaoxiaoNeural", "zh-CN-YunjianNeural"),
    "th": ("th-TH-PremwadeeNeural", "th-TH-NiwatNeural"),
    "vi": ("vi-VN-HoaiMyNeural", "vi-VN-NamMinhNeural"),
    "id": ("id-ID-GadisNeural", "id-ID-ArdiNeural"),
    "ms": ("ms-MY-YasminNeural", "ms-MY-OsmanNeural"),
}

# Sinonimos aceptados para indicar genero de voz.
GENEROS = {
    "f": "female",
    "female": "female",
    "femenina": "female",
    "mujer": "female",
    "m": "male",
    "male": "male",
    "masculina": "male",
    "masculino": "male",
    "hombre": "male",
}

# Indice de genero dentro de las tuplas de VOZES_POR_IDIOMA.
GENERO_INDICE = {"female": 0, "male": 1}

# Genero forzado en caliente. Vacio significa "usar el curado del idioma".
_genero_forzado: str = ""

# Variante regional preferida por idioma, para el caso de que la voz curada
# no exista y haya que elegir otra del mismo idioma.
LOCALE_POR_IDIOMA: dict[str, str] = {
    "es": "es-ES",
    "en": "en-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "ca": "ca-ES",
    "gl": "gl-ES",
    "ru": "ru-RU",
    "uk": "uk-UA",
    "pl": "pl-PL",
    "cs": "cs-CZ",
    "sk": "sk-SK",
    "hu": "hu-HU",
    "ro": "ro-RO",
    "bg": "bg-BG",
    "el": "el-GR",
    "sv": "sv-SE",
    "da": "da-DK",
    "nb": "nb-NO",
    "fi": "fi-FI",
    "nl": "nl-NL",
    "tr": "tr-TR",
    "ar": "ar-EG",
    "he": "he-IL",
    "hi": "hi-IN",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "th": "th-TH",
    "vi": "vi-VN",
    "id": "id-ID",
    "ms": "ms-MY",
}

# Codigos ISO 639-1 a nombre legible, para los mensajes de listar_voces.
IDIOMA_ESPECIAL = {
    "zh": "chino",
    "ja": "japones",
    "ko": "coreano",
    "he": "hebreo",
}


def _normalizar_genero(valor: str) -> str:
    """Normaliza un alias de genero. Vacio si no se reconoce."""
    return GENEROS.get((valor or "").strip().lower(), "")


def _genero_efectivo(idioma: str) -> str:
    """Devuelve el genero de voz que corresponde a un idioma dado.

    Resultado: "female", "male" o "" si el idioma no tiene genero asignado y
    no se ha forzado ninguno.
    """
    if _genero_forzado:
        return _genero_forzado
    configurado = _normalizar_genero(VOICE_GENDER)
    if configurado:
        return configurado

    base = (idioma or "").lower().split("-")[0]
    # Si el idioma tiene par de voces asignado, se usa la femenina por
    # defecto salvo que el usuario indique lo contrario.
    return "female" if base in VOZES_POR_IDIOMA else ""


def _nombre_idioma(idioma: str) -> str:
    """Nombre legible de un codigo ISO, para los mensajes de las herramientas."""
    base = (idioma or "").lower().split("-")[0]
    if base in IDIOMA_ESPECIAL:
        return f"{IDIOMA_ESPECIAL[base]} ({base})"
    return base or "desconocido"


def _cargar_catalogo_edge() -> dict[str, dict]:
    """Descarga y cachea el catalogo de voces de edge-tts."""
    global _catalogo_cargado
    if _catalogo_cargado:
        return _catalogo_edge

    async def _obtener() -> dict[str, dict]:
        import edge_tts

        voces = await edge_tts.list_voices()
        return {v["ShortName"]: v for v in voces}

    try:
        _catalogo_edge.update(asyncio.run(_obtener()))
        _catalogo_cargado = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo descargar el catalogo de voces: %s", exc)
    return _catalogo_edge


def _idioma_del_sistema() -> str:
    """Devuelve el idioma del sistema en formato ISO, vacio si no se puede."""
    import locale

    for getter in (
        lambda: locale.getlocale()[0],
        lambda: locale.getdefaultlocale()[0],
        lambda: os.environ.get("LANG", "").split(".")[0],
    ):
        try:
            valor = getter()
        except Exception:  # noqa: BLE001
            valor = None
        if valor:
            return valor.replace("-", "_").split("_")[0].lower()
    return ""


def _detectar_idioma(texto: str) -> str:
    """Detecta el idioma del texto. Es una aproximacion de fiabilidad limitada.

    langdetect acierta bien en textos de una frase larga, pero en textos muy
    cortos puede fallar con total confianza: "Hecho." lo identifica como
    checo con probabilidad 1.0. Por eso esta deteccion es opt-in mediante
    VOICE_LANGUAGE=auto y nunca es el mecanismo por defecto.
    """
    try:
        from langdetect import DetectorFactory, detect_langs
    except ImportError:
        return ""

    DetectorFactory.seed = 0
    try:
        candidatos = detect_langs(texto)
    except Exception:  # noqa: BLE001 - sin rasgos suficientes
        return ""
    return candidatos[0].lang.split("-")[0].lower() if candidatos else ""


def _resolver_idioma(texto: str) -> str:
    """Determina el idioma de la locucion segun la precedencia configurada."""
    if _idioma_forzado is not None and _idioma_forzado.lower() != "auto":
        return _idioma_forzado

    if _idioma_forzado == "auto" or (
        _idioma_forzado is None and VOICE_LANGUAGE.lower() == "auto"
    ):
        detected = _detectar_idioma(texto)
        if detected:
            return detected
        logger.warning(
            "No se pudo detectar el idioma del texto; se usa '%s'.", DEFAULT_LANGUAGE
        )
        return DEFAULT_LANGUAGE

    if _idioma_forzado is None and VOICE_LANGUAGE:
        return VOICE_LANGUAGE.replace("_", "-").lower()

    return _idioma_del_sistema() or DEFAULT_LANGUAGE


def _elegir_segun_genero(
    catalogo: dict[str, dict], candidatos: list[str], genero: str
) -> str | None:
    """De una lista de voces, devuelve la primera del genero pedido.

    Si ninguna coincide con el genero, devuelve la primera de la lista para no
    dejar al usuario sin voz.
    """
    if not candidatos:
        return None
    if genero:
        for nombre in candidatos:
            if catalogo.get(nombre, {}).get("Gender", "").lower() == genero:
                return nombre
        logger.info(
            "Ninguna voz disponible de '%s' es %s; se usa la primera.",
            ",".join(candidatos[:1]),
            genero,
        )
    return candidatos[0]


def _voz_edge_para_idioma(
    idioma: str, genero: str = "", ignorar_nombre: bool = False
) -> str:
    """Elige una voz de edge-tts para el idioma y el genero pedidos.

    Si se pide una variante regional explicita ("pt-PT", "en-GB") se respeta
    esa region; si solo se pide el idioma ("pt", "en") se usa la variante
    regional preferida de la tabla.

    ignorar_nombre salta la anulacion de VOICE_NAME. Solo lo usan las
    herramientas para poder mostrar al usuario que alternativas existirian si
    quitase esa variable.
    """
    if VOICE_NAME and not ignorar_nombre:
        return VOICE_NAME

    clave = idioma.lower()
    base, _, region = clave.partition("-")
    genero = genero or _genero_efectivo(clave)

    catalogo = _cargar_catalogo_edge()
    if not catalogo:
        # Sin catalogo solo se puede usar la voz curada, si la hay.
        par = VOZES_POR_IDIOMA.get(base)
        if par:
            return par[GENERO_INDICE.get(genero, 0)]
        return DEFAULT_EDGE_VOICE

    por_locale: dict[str, list[str]] = {}
    for nombre, info in catalogo.items():
        por_locale.setdefault(info.get("Locale", "").lower(), []).append(nombre)

    par = VOZES_POR_IDIOMA.get(base)
    indice = GENERO_INDICE.get(genero, 0)
    curada = par[indice] if par else None

    # 1. Voz curada del idioma, si su variante regional es la pedida.
    if region and curada and curada in catalogo:
        if catalogo[curada].get("Locale", "").lower() == clave:
            return curada

    # 2. Voz del genero pedido dentro de la region pedida.
    if region:
        elegida = _elegir_segun_genero(catalogo, por_locale.get(clave, []), genero)
        if elegida:
            return elegida

    # 3. Voz curada del idioma con el genero pedido.
    if curada and curada in catalogo:
        return curada

    # 4. Variante regional preferida del idioma.
    regional = (LOCALE_POR_IDIOMA.get(base) or "").lower()
    elegida = _elegir_segun_genero(catalogo, por_locale.get(regional, []), genero)
    if elegida:
        return elegida

    # 5. Cualquier voz del idioma, en cualquier variante regional.
    todos = [
        nombre
        for nombre, info in catalogo.items()
        if info.get("Locale", "").split("-")[0].lower() == base
    ]
    elegida = _elegir_segun_genero(catalogo, todos, genero)
    if elegida:
        return elegida

    logger.warning(
        "No hay voces de edge-tts para '%s'; se usa '%s'.", idioma, DEFAULT_EDGE_VOICE
    )
    return DEFAULT_EDGE_VOICE


def _voz_sapi5_para_idioma(idioma: str, engine) -> str | None:
    """Elige una voz de SAPI5 para el idioma pedido, o None si no hay ninguna.

    Windows nombra las voces con codigo de region (TTS_MS_ES-ES_HELENA), asi
    que se busca primero la variante regional exacta y despues el idioma.
    """
    voces = engine.getProperty("voices")

    def coincide(voz) -> bool:
        return idioma.lower() in f"{voz.id} {voz.name}".lower()

    if VOICE_NAME:
        objetivo = VOICE_NAME.lower()
        for voice in voces:
            if objetivo in f"{voice.id} {voice.name}".lower():
                return voice.id
        return None

    clave = idioma.lower()
    base, _, region = clave.partition("-")

    # 1. Variante regional exacta, si se ha pedido.
    if region:
        for voice in voces:
            if coincide(voice):
                return voice.id

    # 2. Cualquier variante del idioma.
    for patron in (f"{base}-", f"_{base}-", f" {base} "):
        for voice in voces:
            if patron in f"{voice.id} {voice.name}".lower():
                return voice.id

    # 3. El idioma aparece como token suelto en el identificador.
    for voice in voces:
        if base in f"{voice.id} {voice.name}".lower().replace("_", "-").split():
            return voice.id
    return None


# --------------------------------------------------------------------------
# Motor SAPI5 (offline)
# --------------------------------------------------------------------------
def _inicializar_sapi5():
    """Crea el engine pyttsx3. La voz se ajusta en cada locucion segun idioma."""
    global _sapi5_engine
    with _lock:
        if _sapi5_engine is not None:
            return _sapi5_engine

        import pyttsx3

        engine = pyttsx3.init()
        _sapi5_engine = engine
        return engine


def _aplicar_voz_sapi5(engine, idioma: str) -> None:
    """Fija en el engine la voz del idioma, y solo si ha cambiado."""
    global _voz_sapi5_actual

    objetivo = _voz_sapi5_para_idioma(idioma, engine)
    if objetivo is None:
        if _voz_sapi5_actual is None:
            engine.setProperty("voice", engine.getProperty("voice"))
            _voz_sapi5_actual = engine.getProperty("voice")
        logger.warning(
            "SAPI5 no tiene voces para '%s'; se mantiene la actual.", idioma
        )
        return

    if objetivo != _voz_sapi5_actual:
        engine.setProperty("voice", objetivo)
        _voz_sapi5_actual = objetivo


def _hablar_sapi5(texto: str) -> None:
    engine = _inicializar_sapi5()
    _aplicar_voz_sapi5(engine, _resolver_idioma(texto))
    engine.say(texto)
    engine.runAndWait()
    # runAndWait puede retornar antes de que el sintetizador termine. Sin esta
    # espera el mensaje siguiente interrumpe al anterior.
    while engine.isBusy():
        time.sleep(0.1)


# --------------------------------------------------------------------------
# Motor edge-tts (red, voces neuronales)
# --------------------------------------------------------------------------
async def _generar_mp3_async(texto: str, destino: str, voz: str) -> None:
    import edge_tts

    comunicacion = edge_tts.Communicate(
        texto,
        voz,
        rate=_normalizar_porcentaje(VOICE_RATE),
        volume=_normalizar_porcentaje(VOICE_VOLUME),
    )
    await comunicacion.save(destino)


def _hablar_edge(texto: str) -> None:
    """Genera el MP3 con edge-tts y lo reproduce."""
    idioma = _resolver_idioma(texto)
    voz = _voz_edge_para_idioma(idioma)
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "voz.mp3")
        asyncio.run(_generar_mp3_async(texto, ruta, voz))
        _reproducir_mp3(ruta)


# --------------------------------------------------------------------------
# Hilo trabajador
# --------------------------------------------------------------------------
def _trabajador_voz() -> None:
    """Consume la cola y reproduce cada resumen con el motor configurado."""
    _hablar = _hablar_edge if ENGINE == "edge" else _hablar_sapi5

    while True:
        texto = _cola.get()
        if texto is None:
            _cola.task_done()
            return
        try:
            _hablar(texto)
        except Exception:  # noqa: BLE001 - la voz nunca debe tumbar el servidor
            logger.exception("No se pudo reproducir el resumen: %s", texto)
        finally:
            _cola.task_done()


_hilo = threading.Thread(target=_trabajador_voz, name="voz", daemon=True)
_hilo.start()


# --------------------------------------------------------------------------
# Herramientas
# --------------------------------------------------------------------------
@mcp.tool()
def reproducir_resumen_voz(texto: str) -> str:
    """
    Reproduce un resumen en voz alta a traves de los altavoces del sistema.

    Ajusta la longitud del resumen a la cantidad de trabajo realizado: una frase
    corta para un cambio puntual, y un resumen mas desarrollado cuando la tarea
    ha sido grande o ha tenido varios pasos. No narres codigo literal ni
    detalles que no aporten valor; escribe en primera persona.

    Si el resumen supera el tope de seguridad (MAX_SUMMARY_WORDS, 400 por
    defecto) se recorta automaticamente.

    El idioma de la voz se elige solo: por defecto el del sistema, o el que se
    haya fijado con establecer_idioma. No hace falta indicar el idioma.
    """
    limpio = " ".join(texto.split()).strip()
    if not limpio:
        return "No se reprodujo nada: el resumen estaba vacio."

    palabras = limpio.split()
    recortado = False
    try:
        tope = int(MAX_SUMMARY_WORDS)
    except ValueError:
        logger.warning("MAX_SUMMARY_WORDS invalido: %s", MAX_SUMMARY_WORDS)
        tope = 400

    if tope > 0 and len(palabras) > tope:
        limpio = " ".join(palabras[:tope]).rstrip(" ,;:.-")
        recortado = True
        logger.warning(
            "Resumen recortado a %d palabras (envio %d)", tope, len(palabras)
        )

    respuesta = f"Reproduciendo resumen con exito: {limpio}"
    if recortado:
        respuesta += (
            f" [Aviso: se recorto a {tope} palabras por seguridad. Divide el "
            f"trabajo en varias llamadas si necesitas mas detalle.]"
        )
    _cola.put(limpio)
    return respuesta


@mcp.tool()
def listar_voces() -> str:
    """
    Muestra que idioma y voz estan en uso ahora mismo, y las voces de
    sintesis instaladas en el sistema.
    """
    import sys

    idioma = _resolver_idioma("")
    origen_idioma = (
        "forzado en caliente"
        if _idioma_forzado is not None
        else VOICE_LANGUAGE or "idioma del sistema"
    )
    cabecera = [
        f"Motor: {ENGINE}",
        f"Idioma: {_nombre_idioma(idioma)} (origen: {origen_idioma})",
    ]

    if ENGINE == "edge":
        genero = _genero_efectivo(idioma) or "sin preferencia"
        cabecera.append(f"Genero: {genero}")
        cabecera.append(f"Voz en uso: {_voz_edge_para_idioma(idioma, genero)}")
        cabecera.append("")
        if VOICE_NAME:
            cabecera.append(
                f"AVISO: VOICE_NAME esta fijado a {VOICE_NAME}, asi que anula la "
                f"seleccion automatica de idioma y genero. Estas son las voces "
                f"que se usarian si lo quitaras:"
            )
        else:
            cabecera.append("Opciones de genero para el idioma actual:")
        for etiqueta, valor in (("femenina", "female"), ("masculina", "male")):
            cabecera.append(
                f"- {etiqueta}: "
                f"{_voz_edge_para_idioma(idioma, valor, ignorar_nombre=True)}"
            )
        cabecera.append("")
        cabecera.append(
            "Idiomas con voz curada (femenina y masculina): "
            + ", ".join(sorted(VOZES_POR_IDIOMA))
        )
        cabecera.append(
            "Para el catalogo completo de voces neuronales ejecuta: "
            "python -m edge_tts --list-voices"
        )
        return "\n".join(cabecera)

    try:
        engine = _inicializar_sapi5()
    except Exception as exc:  # noqa: BLE001
        cabecera.append(
            f"No se pudo inicializar el motor sapi5 en {sys.platform}: {exc}. "
            f"Comprueba las dependencias de voz del sistema o cambia "
            f"VOICE_ENGINE a 'edge'."
        )
        return "\n".join(cabecera)

    actual = engine.getProperty("voice")
    cabecera.append(f"Voz seleccionada: {actual}")
    cabecera.append("")
    cabecera.append("Voces instaladas:")
    for voice in engine.getProperty("voices"):
        marca = " (seleccionada)" if voice.id == actual else ""
        cabecera.append(f"- {voice.id.split(chr(92))[-1]} | {voice.name}{marca}")
    if sys.platform == "win32":
        cabecera.append("")
        cabecera.append(
            "Voces adicionales de mejor calidad (Microsoft Laura, Microsoft Pablo) "
            "requieren registrar las claves OneCore; ejecuta "
            "registrar_voces_onecore.ps1 como administrador."
        )
    return "\n".join(cabecera)


@mcp.tool()
def establecer_idioma(idioma: str, genero: str = "") -> str:
    """
    Fija el idioma y, opcionalmente, el genero de la voz para las siguientes
    locuciones, sin editar la configuracion. Llama a esta herramienta cuando el
    usuario pida hablar en otro idioma o con otra voz, por ejemplo "habla en
    ingles", "cambia a frances" o "usa una voz masculina".

    Acepta un codigo ISO 639-1 ("es", "en", "fr") o con variante regional
    ("pt-BR", "en-GB"). El genero admite "female"/"male", "f"/"m",
    "femenina"/"masculina" y "mujer"/"hombre". Usa "" para volver al idioma del
    sistema, o "auto" para deducir el idioma del texto de cada resumen.
    """
    global _idioma_forzado, _genero_forzado

    valor_genero = _normalizar_genero(genero)
    if genero.strip() and not valor_genero:
        return (
            f"Genero no reconocido: {genero}. Usa 'female' o 'male' "
            f"(tambien 'femenina'/'masculina' o 'mujer'/'hombre')."
        )
    if valor_genero:
        _genero_forzado = valor_genero

    valor = (idioma or "").strip()
    if not valor:
        _idioma_forzado = None
        genero_texto = (
            f", genero {valor_genero}" if valor_genero else ", genero sin forzar"
        )
        return (
            f"Idioma restablecido al del sistema: "
            f"{_nombre_idioma(_idioma_del_sistema() or DEFAULT_LANGUAGE)}{genero_texto}."
        )

    if valor.lower() == "auto":
        _idioma_forzado = "auto"
        return (
            "Deteccion automatica activada. El idioma se deducira del texto de cada "
            "resumen. Ten en cuenta que con resúmenes muy cortos la deteccion puede "
            "fallar, porque las bibliotecas de idioma resuelven con certeza "
            "equivocada. Para un idioma fijo es mas fiable usar su codigo."
        )

    valor = valor.replace("_", "-")
    if not valor.replace("-", "").isalnum():
        return f"Idioma invalido: {idioma}. Usa un codigo como 'es', 'en' o 'pt-BR'."

    _idioma_forzado = valor

    if ENGINE == "edge":
        genero_efectivo = valor_genero or _genero_efectivo(valor.lower())
        voz = _voz_edge_para_idioma(valor.lower(), genero_efectivo)
        return (
            f"Idioma fijado a {_nombre_idioma(valor)}. "
            f"Voz seleccionada: {voz} ({genero_efectivo})."
        )
    aviso = "" if valor_genero else " El genero no se aplica a sapi5."
    return (
        f"Idioma fijado a {_nombre_idioma(valor)}. En el motor sapi5 se usara la "
        f"voz de ese idioma si esta instalada en el sistema.{aviso}"
    )


@mcp.tool()
def establecer_genero(genero: str) -> str:
    """
    Cambia el genero de la voz sin tocar el idioma. Llama a esta herramienta
    cuando el usuario pida una voz masculina o femenina, por ejemplo "usa voz
    de hombre".

    Acepta "female"/"male", "f"/"m", "femenina"/"masculina" y
    "mujer"/"hombre". Usa "" para volver al genero por defecto del idioma.
    """
    global _genero_forzado

    if not (genero or "").strip():
        _genero_forzado = ""
        return "Genero restablecido al valor por defecto del idioma."

    valor = _normalizar_genero(genero)
    if not valor:
        return (
            f"Genero no reconocido: {genero}. Usa 'female' o 'male' "
            f"(tambien 'femenina'/'masculina' o 'mujer'/'hombre')."
        )

    _genero_forzado = valor
    if ENGINE != "edge":
        return (
            f"Genero fijado a {valor}, pero el motor sapi5 no expone el genero de "
            f"sus voces, asi que no se aplicara. Usa el motor edge para elegir voz "
            f"por genero."
        )

    idioma = _resolver_idioma("")
    voz = _voz_edge_para_idioma(idioma, valor)
    if VOICE_NAME:
        alternativa = _voz_edge_para_idioma(idioma, valor, ignorar_nombre=True)
        return (
            f"Genero registrado como {valor}, pero VOICE_NAME sigue fijando la voz "
            f"a {VOICE_NAME}, asi que el cambio no se oye. Quita VOICE_NAME de la "
            f"configuracion para poder elegir por genero; entonces se usaria "
            f"{alternativa}."
        )
    return f"Genero fijado a {valor}. Voz para {_nombre_idioma(idioma)}: {voz}."


if __name__ == "__main__":
    mcp.run()