"""
Servidor MCP de resumen por voz.

Expone una herramienta que lee en voz alta un resumen breve de la accion
recien realizada. Soporta dos motores de sintesis, seleccionables por
variables de entorno:

    VOICE_ENGINE = "sapi5" (por defecto) | "edge"
    VOICE_NAME   = nombre o id de la voz (ver listar_voces)
    VOICE_RATE   = velocidad, +/- porcentaje sobre la defecto (ej. "+10%", "-15%")
    VOICE_VOLUME = volumen 0-100 (por defecto 100)

Motores:
- sapi5: pyttsx3 sobre el sintetizador nativo de Windows. Offline y gratuito,
  pero las voces disponibles son limited y de calidad basica.
- edge:  edge-tts (voces neuronales de Azure). Calidad muy superior, pero
  requiere conexion a internet en cada locucion.

Notas de implementacion:
- mcp 2.x: FastMCP fue renombrado a MCPServer (mcp.server.mcpserver).
- El motor se inicializa una sola vez y se usa desde un unico hilo
  trabajador. pyttsx3 no es thread-safe y COM es apartment-threaded: crear el
  engine en un hilo y usarlo en otro bloquea el proceso indefinidamente.
- Las peticiones se encolan para que dos resumenes consecutivos no se pisen.
- El hilo trabajador es daemon para no bloquear el cierre del proceso.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import queue
import tempfile
import threading
import time

import pyttsx3
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("VoiceSummaryServer")

logger = logging.getLogger("voice-summary")

ENGINE = os.environ.get("VOICE_ENGINE", "sapi5").strip().lower()
VOICE_NAME = os.environ.get("VOICE_NAME", "").strip()
VOICE_RATE = os.environ.get("VOICE_RATE", "+0%").strip()
VOICE_VOLUME = os.environ.get("VOICE_VOLUME", "100").strip()


def _normalizar_porcentaje(valor: str, defecto: int = 0) -> str:
    """Convierte un valor de volumen/velocidad al formato que exige edge-tts.

    edge-tts solo admite el formato relativo "+10%" / "-10%". SAPI5 en cambio
    usa una escala absoluta donde 100 es el valor normal. Aceptamos ambas
    notaciones para que la misma variable sirva segun el motor: un numero
    desnudo se interpreta como escala absoluta (100 = normal) y se traduce a la
    variacion relativa equivalente.
    """
    texto = valor.strip()
    if texto.endswith("%"):
        return texto
    try:
        absoluto = int(float(texto))
    except ValueError:
        return f"{defecto:+d}%"
    return f"{absoluto - 100:+d}%"

# Voces por defecto segun motor.
DEFAULT_SAPI5_VOICE = "es-es"  # se busca por fragmento de id o nombre
DEFAULT_EDGE_VOICE = "es-ES-ElviraNeural"

# Peticiones de voz pendientes. El hilo trabajador las consume en orden.
_cola: queue.Queue[str | None] = queue.Queue()

# Estado del motor, inicializado de forma perezosa desde el hilo trabajador.
_lock = threading.Lock()
_sapi5_engine: pyttsx3.Engine | None = None


# --------------------------------------------------------------------------
# Motor SAPI5 (offline)
# --------------------------------------------------------------------------
def _inicializar_sapi5() -> pyttsx3.Engine:
    """Crea el engine SAPI5 y selecciona voz por fragmento de id o nombre."""
    global _sapi5_engine
    with _lock:
        if _sapi5_engine is not None:
            return _sapi5_engine
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
    # runAndWait puede retornar antes de que SAPI5 termine. Sin esta espera el
    # mensaje siguiente interrumpe al anterior.
    while engine.isBusy():
        time.sleep(0.1)


# --------------------------------------------------------------------------
# Motor edge-tts (red, voces neuronales)
# --------------------------------------------------------------------------
def _mci(comando: str) -> None:
    """Envia un comando MCI de Windows (reproductor de audio integrated)."""
    ctypes.windll.winmm.mciSendStringW(ctypes.c_wchar_p(comando), None, 0, 0)


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
    """Genera el MP3 con edge-tts y lo reproduce con MCI."""
    with tempfile.TemporaryDirectory() as tmp:
        ruta = os.path.join(tmp, "voz.mp3").replace("\\", "\\\\")
        asyncio.run(_generar_mp3_async(texto, ruta))
        _mci(f'open "{ruta}" type mpegvideo alias resumen')
        try:
            _mci("play resumen wait")
        finally:
            _mci("close resumen")


# --------------------------------------------------------------------------
# Hilo trabajador
# --------------------------------------------------------------------------
def _trabajador_voz() -> None:
    """Consume la cola y reproduce cada resumen con el motor configurado."""
    if ENGINE == "edge":
        _hablar = _hablar_edge
    else:
        _hablar = _hablar_sapi5

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
    Llama a esta herramienta con un resumen muy breve (maximo 10-15 palabras)
    de la accion o tarea que acabas de realizar, en primera persona y sin
    narrar codigo literal.
    """
    limpio = " ".join(texto.split()).strip()
    if not limpio:
        return "No se reprodujo nada: el resumen estaba vacio."

    _cola.put(limpio)
    return f"Reproduciendo resumen con exito: {limpio}"


@mcp.tool()
def listar_voces() -> str:
    """
    Lista las voces de sintesis disponibles en el sistema, marcando cual es la
    voz configurada ahora mismo. Usa el resultado para elegir VOICE_NAME.
    """
    if ENGINE == "edge":
        return (
            f"Motor edge-tts activo. Voz configurada: "
            f"{VOICE_NAME or DEFAULT_EDGE_VOICE}. Usa /mcps o la documentacion de "
            f"edge-tts para ver el catalogo completo de voces neuronales."
        )

    engine = _inicializar_sapi5()
    actual = engine.getProperty("voice")
    lineas = [f"Motor SAPI5 activo. Voz seleccionada: {actual}", "Voces instaladas:"]
    for voice in engine.getProperty("voices"):
        marca = " (seleccionada)" if voice.id == actual else ""
        lineas.append(f"- {voice.id.split(chr(92))[-1]} | {voice.name}{marca}")
    lineas.append(
        "Voces adicionales de mayor calidad (Microsoft Laura, Microsoft Pablo) "
        "requieren registrar las claves OneCore; ejecuta registrar_voces_onecore.ps1 "
        "como administrador."
    )
    return "\n".join(lineas)


if __name__ == "__main__":
    mcp.run()