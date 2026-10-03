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

# Tope de seguridad de palabras por resumen. No es una recomendacion de
# longitud: el resumen lo decide el asistente segun la cantidad de trabajo que
# haya hecho. Este limite solo evita locuciones accidentales de varios minutos
# por un `texto` desmedido. Por defecto 400 palabras, unos 2-3 minutos.
MAX_SUMMARY_WORDS = os.environ.get("MAX_SUMMARY_WORDS", "400").strip()

# Voces por defecto segun motor.
DEFAULT_SAPI5_VOICE = "es-es"  # se busca por fragmento de id o nombre
DEFAULT_EDGE_VOICE = "es-ES-AlvaroNeural"

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
# Motor SAPI5 (offline)
# --------------------------------------------------------------------------
def _inicializar_sapi5():
    """Crea el engine pyttsx3 y selecciona voz por fragmento de id o nombre."""
    global _sapi5_engine
    with _lock:
        if _sapi5_engine is not None:
            return _sapi5_engine

        import pyttsx3

        engine = pyttsx3.init()
        objetivo = (VOICE_NAME or DEFAULT_SAPI5_VOICE).lower()
        for voice in engine.getProperty("voices"):
            if objetivo in f"{voice.id} {voice.name}".lower():
                engine.setProperty("voice", voice.id)
                break
        else:
            disponible = [v.id.split("\\")[-1] for v in engine.getProperty("voices")]
            logger.warning(
                "Voz SAPI5 '%s' no encontrada. Disponibles: %s", objetivo, disponible
            )

        _sapi5_engine = engine
        return engine


def _hablar_sapi5(texto: str) -> None:
    engine = _inicializar_sapi5()
    engine.say(texto)
    engine.runAndWait()
    # runAndWait puede retornar antes de que el sintetizador termine. Sin esta
    # espera el mensaje siguiente interrumpe al anterior.
    while engine.isBusy():
        time.sleep(0.1)


# --------------------------------------------------------------------------
# Motor edge-tts (red, voces neuronales)
# --------------------------------------------------------------------------
async def _generar_mp3_async(texto: str, destino: str) -> None:
    import edge_tts

    comunicacion = edge_tts.Communicate(
        texto,
        VOICE_NAME or DEFAULT_EDGE_VOICE,
        rate=_normalizar_porcentaje(VOICE_RATE),
        volume=_normalizar_porcentaje(VOICE_VOLUME),
    )
    await comunicacion.save(destino)


def _hablar_edge(texto: str) -> None:
    """Genera el MP3 con edge-tts y lo reproduce."""
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "voz.mp3")
        asyncio.run(_generar_mp3_async(texto, ruta))
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
    Lista las voces de sintesis disponibles en el sistema, marcando cual es la
    voz configurada ahora mismo. Usa el resultado para elegir VOICE_NAME.
    """
    import sys

    if ENGINE == "edge":
        return (
            f"Motor edge-tts activo. Voz configurada: "
            f"{VOICE_NAME or DEFAULT_EDGE_VOICE}. "
            f"Para ver el catalogo completo de voces neuronales ejecuta: "
            f"python -m edge_tts --list-voices"
        )

    try:
        engine = _inicializar_sapi5()
    except Exception as exc:  # noqa: BLE001
        return (
            f"No se pudo inicializar el motor sapi5 en {sys.platform}: {exc}. "
            f"Comprueba las dependencias de voz del sistema o cambia "
            f"VOICE_ENGINE a 'edge'."
        )

    actual = engine.getProperty("voice")
    lineas = [f"Motor SAPI5 activo. Voz seleccionada: {actual}", "Voces instaladas:"]
    for voice in engine.getProperty("voices"):
        marca = " (seleccionada)" if voice.id == actual else ""
        lineas.append(f"- {voice.id.split(chr(92))[-1]} | {voice.name}{marca}")
    if sys.platform == "win32":
        lineas.append(
            "Voces adicionales de mejor calidad (Microsoft Laura, Microsoft Pablo) "
            "requieren registrar las claves OneCore; ejecuta "
            "registrar_voces_onecore.ps1 como administrador."
        )
    return "\n".join(lineas)


if __name__ == "__main__":
    mcp.run()