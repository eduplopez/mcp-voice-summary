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
# Voz neuronal preferida por idioma. Los nombres se han verificado contra el
# catalogo real de edge-tts; si alguno dejara de existir, la busqueda cae
# automaticamente a cualquier otra voz del mismo idioma.
VOZ_POR_IDIOMA: dict[str, str] = {
    "es": "es-ES-ElviraNeural",
    "en": "en-US-AriaNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "it": "it-IT-ElsaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ca": "ca-ES-JoanaNeural",
    "gl": "gl-ES-CeldeNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "uk": "uk-UA-PolinaNeural",
    "pl": "pl-PL-ZofiaNeural",
    "cs": "cs-CZ-VlastaNeural",
    "sk": "sk-SK-LukasNeural",
    "hu": "hu-HU-TamasNeural",
    "ro": "ro-RO-EmilNeural",
    "bg": "bg-BG-KalinaNeural",
    "el": "el-GR-NestorasNeural",
    "sv": "sv-SE-SofieNeural",
    "da": "da-DK-JeppeNeural",
    "nb": "nb-NO-PernilleNeural",
    "fi": "fi-FI-NooraNeural",
    "nl": "nl-NL-MaartenNeural",
    "tr": "tr-TR-AhmetNeural",
    "ar": "ar-EG-ShakirNeural",
    "he": "he-IL-HilaNeural",
    "hi": "hi-IN-SwaraNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "th": "th-TH-PremwadeeNeural",
    "vi": "vi-VN-HoaiMyNeural",
    "id": "id-ID-GadisNeural",
    "ms": "ms-MY-YasminNeural",
}

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


def _voz_edge_para_idioma(idioma: str) -> str:
    """Elige una voz de edge-tts para el idioma pedido.

    Si se pide una variante regional explicita ("pt-PT", "en-GB") se respeta
    esa region; si solo se pide el idioma ("pt", "en") se usa la variante
    regional preferida de la tabla.
    """
    if VOICE_NAME:
        return VOICE_NAME

    clave = idioma.lower()
    base, _, region = clave.partition("-")

    catalogo = _cargar_catalogo_edge()
    if not catalogo:
        # Sin catalogo solo se puede usar la voz curada, si la hay.
        return VOZ_POR_IDIOMA.get(base, DEFAULT_EDGE_VOICE)

    curada = VOZ_POR_IDIOMA.get(base)
    por_locale = {
        nombre: info.get("Locale", "") for nombre, info in catalogo.items()
    }

    if region:
        # 1. Voz curada del idioma, pero solo si es de la region pedida.
        if curada and por_locale.get(curada, "").lower() == clave:
            return curada
        # 2. Cualquier voz de esa region exacta.
        for nombre, locale in por_locale.items():
            if locale.lower() == clave:
                return nombre

    # 3. Voz curada del idioma.
    if curada and curada in catalogo:
        return curada

    # 4. Variante regional preferida del idioma.
    regional = LOCALE_POR_IDIOMA.get(base)
    if regional:
        for nombre, locale in por_locale.items():
            if locale.lower() == regional.lower():
                return nombre

    # 5. Cualquier voz del idioma, en cualquier variante regional.
    for nombre, locale in por_locale.items():
        if locale.split("-")[0].lower() == base:
            return nombre

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
    forzado = "forzado en caliente" if _idioma_forzado is not None else VOICE_LANGUAGE or "idioma del sistema"
    cabecera = [
        f"Motor: {ENGINE}",
        f"Idioma: {_nombre_idioma(idioma)} (origen: {forzado})",
    ]

    if ENGINE == "edge":
        voz = _voz_edge_para_idioma(idioma)
        cabecera.append(f"Voz para ese idioma: {voz}")
        if VOICE_NAME:
            cabecera.append(f"VOICE_NAME sobrescribe la eleccion automatica: {VOICE_NAME}")
        cabecera.append("")
        cabecera.append(
            "Idiomas con voz curada: " + ", ".join(sorted(VOZ_POR_IDIOMA))
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
def establecer_idioma(idioma: str) -> str:
    """
    Fija el idioma de la voz para las siguientes locuciones, sin necesidad de
    editar la configuracion. Llama a esta herramienta cuando el usuario pida
    hablar en otro idioma, por ejemplo "habla en ingles" o "cambia a frances".

    Acepta un codigo ISO 639-1 ("es", "en", "fr") o con variante regional
    ("pt-BR", "en-GB"). Usa "auto" para detectar el idioma de cada resumen a
    partir del texto, o "auto" con "system" para volver al idioma del sistema.
    """
    global _idioma_forzado

    valor = (idioma or "").strip()
    if not valor:
        _idioma_forzado = None
        return (
            f"Idioma restablecido al del sistema: {_idioma_del_sistema() or DEFAULT_LANGUAGE}."
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
        voz = _voz_edge_para_idioma(valor.lower())
        return f"Idioma fijado a {_nombre_idioma(valor)}. Voz seleccionada: {voz}."
    return (
        f"Idioma fijado a {_nombre_idioma(valor)}. En el motor sapi5 se usara la "
        f"voz de ese idioma si esta instalada en el sistema."
    )


if __name__ == "__main__":
    mcp.run()