# Servidor MCP de Resumen por Voz

Un servidor [MCP](https://modelcontextprotocol.io/) local que lee en voz alta un
resumen de las acciones que un asistente de IA acaba de realizar en el código.
Pensado como capa de accesibilidad: el usuario oye qué ha hecho el agente sin
necesidad de leer la respuesta completa.

Funciona con dos motores de síntesis, incluido un motor offline.

## Que hace

Expone una herramienta MCP, `reproducir_resumen_voz`, que el asistente invoca
tras modificar código, crear archivos o ejecutar comandos.

La longitud del resumen la decide el asistente según cuánto trabajo haya
hecho: una frase corta para un cambio puntual, o un resumen más desarrollado
cuando la tarea ha sido grande o ha tenido varios pasos. Ver
[Controlar la longitud del resumen](#controlar-la-longitud-del-resumen).

La reproducción es asíncrona: la herramienta encola el texto y devuelve el
control de inmediato, de modo que la locución nunca bloquea al asistente.

## Requisitos

- Python 3.10 o superior.
- Windows, Linux o macOS.

Solo el motor `sapi5` necesita una dependencia del sistema (el sintetizador
nativo). El motor `edge` no necesita nada más que Python.

## Instalacion

```sh
git clone https://github.com/eduplopez/mcp-voice-summary.git
cd mcp-voice-summary

python -m venv .venv
```

Activa el entorno virtual:

```sh
# Windows
.venv\Scripts\activate
# Linux y macOS
source .venv/bin/activate
```

Instala las dependencias:

```sh
pip install -r requirements.txt
```

Comprueba que arranca:

```sh
python server.py
```

El servidor habla por stdio, asi que no veras nada en la consola. Eso es
correcto: cualquier texto que imprima en stdout romperia el protocolo. Ctrl+C
para salir.

### Dependencias del sistema por motor

`requirements.txt` instala solo lo necesario para el motor `edge`, que es
multiplataforma y no depende del sistema. Si quieres usar `sapi5`, instala
ademas `pyttsx3` y el sintetizador correspondiente:

| Sistema | Sintetizador | Instalacion |
| --- | --- | --- |
| Windows | SAPI5 | `pip install pyttsx3 pywin32 comtypes` |
| Linux | NSSpeech | `pip install pyttsx3` y `sudo apt install espeak-ng libespeak-ng1` |
| macOS | NSSS | `pip install pyttsx3` |

### Reproductores de audio

El motor `edge` genera un MP3 que hay que reproducir. El servidor busca un
reproductor disponible y usa el primero que encuentre:

| Sistema | Reproductor | Estado |
| --- | --- | --- |
| Windows | MCI (integrado) | Siempre disponible |
| macOS | `afplay` | Incluido de serie en macOS |
| Linux | `ffplay`, `mpg123`, `cvlc` o `paplay` | Instala al menos uno |

En Linux instala el reproductor que prefieras:

```sh
sudo apt install ffmpeg     # aporta ffplay
# o
sudo apt install mpg123
```

Si tienes otro reproductor, indícalo con `VOICE_PLAYER`:

```json
"environment": { "VOICE_PLAYER": "mi-reproductor" }
```

## Configuracion

Todo se controla por variables de entorno.

| Variable | Valores | Por defecto | Descripcion |
| --- | --- | --- | --- |
| `VOICE_ENGINE` | `edge` o `sapi5` | `sapi5` | Motor de síntesis |
| `VOICE_NAME` | id o nombre de voz | ver abajo | Voz concreta |
| `VOICE_RATE` | `100`, `110`, `-15%` | `100` | Velocidad |
| `VOICE_VOLUME` | `100` | `100` | Volumen |
| `VOICE_PLAYER` | ruta o nombre | autodetectado | Reproductor de MP3 forzado |
| `VOICE_LANGUAGE` | `es`, `en`, `pt-BR`, `auto` | idioma del sistema | Idioma de la voz |
| `MAX_SUMMARY_WORDS` | entero | `400` | Tope de seguridad de palabras por resumen |

`VOICE_RATE` y `VOICE_VOLUME` aceptan notación absoluta (escala SAPI5, donde
100 es el valor normal) y relativa (`+10%`, `-15%`). El servidor traduce
automáticamente al formato que exige cada motor.

Tras cambiar la configuración, reinicia el cliente MCP.

## Motores de sintesis

### edge (recomendado)

Voces neuronales de Azure mediante `edge_tts`. Calidad muy superior a las voces
nativas, a cambio de **requerir conexión a internet en cada locución**, porque
el audio se genera en la nube.

```json
"environment": {
  "VOICE_ENGINE": "edge",
  "VOICE_NAME": "es-ES-AlvaroNeural"
}
```

### sapi5

`pyttsx3` sobre el sintetizador nativo del sistema. **Offline y sin latencia**,
pero las voces disponibles son de calidad básica. Usa SAPI5 en Windows,
NSSpeech en Linux y NSSS en macOS.

```json
"environment": {
  "VOICE_ENGINE": "sapi5",
  "VOICE_NAME": "es-es"
}
```

El valor por defecto de `VOICE_NAME` en este motor es `es-es`, que busca una voz
española por fragmento de identificador. En Windows eso es Helena; en Linux y
macOS el nombre del motor nativo puede ser distinto, así que conviene usar
`listar_voces` para ver qué hay instalado.

## Idiomas

**No hace falta configurar nada para que funcione en tu idioma.** El servidor
elige la voz solo. La precedencia es:

1. `VOICE_NAME` si está definido: gana siempre, es una anulación manual.
2. `establecer_idioma`, si el asistente lo ha llamado durante la sesión.
3. `VOICE_LANGUAGE`, si está definido en la configuración.
4. El idioma del sistema operativo.
5. Inglés, como último recurso.

### Cambiar de idioma

En la configuración, de forma permanente:

```json
"environment": { "VOICE_LANGUAGE": "fr" }
```

O en caliente, sin editar nada. El usuario puede pedirlo en lenguaje natural y
el asistente llama a la herramienta:

```
establecer_idioma("en")      -> Idioma fijado a en. Voz seleccionada: en-US-AriaNeural.
establecer_idioma("pt-BR")   -> Idioma fijado a pt-br. Voz seleccionada: pt-BR-FranciscaNeural.
establecer_idioma("")        -> vuelve al idioma del sistema
```

El cambio en caliente se aplica a las locuciones siguientes y se mantiene hasta
que se reinicie el servidor o se llame de nuevo con otro valor.

### Detección automática del idioma

`VOICE_LANGUAGE=auto` hace que el servidor deduzca el idioma de cada resumen a
partir de su texto.

**Usa esta opción con precaución.** La detección de idioma no es fiable con
textos cortos, y este es justo el caso de uso principal del proyecto.
Medido con resúmenes reales:

| Texto | Idioma real | Idioma detectado |
| --- | --- | --- |
| `"Hecho."` | español | **checo** (con 100 % de confianza) |
| `"Listo"` | español | **alemán** |
| `"Test 123"` | cualquiera | **francés** |
| `"He actualizado el endpoint de login y corregido las dependencias"` | español | español |

Los detectores devuelven una confianza alta incluso cuando se equivocan, así
que no hay forma de filtrar los errores por probabilidad. En resumen: la
detección automática acierta con frases largas y falla con las cortas. Para un
idioma fijo, `VOICE_LANGUAGE` es siempre más fiable.

Si `langdetect` no está instalado, el modo `auto` avisa por log y cae al
inglés. El resto de modos funcionan sin esa dependencia.

### Cobertura

Con el motor `edge` hay **142 locales** disponibles en 322 voces. Los 34
idiomas siguientes tienen una voz curada, con la variante regional que se
indica:

| Idioma | Voz por defecto | Idioma | Voz por defecto |
| --- | --- | --- | --- |
| `es` | `es-ES-ElviraNeural` | `da` | `da-DK-JeppeNeural` |
| `en` | `en-US-AriaNeural` | `fi` | `fi-FI-NooraNeural` |
| `fr` | `fr-FR-DeniseNeural` | `nl` | `nl-NL-MaartenNeural` |
| `de` | `de-DE-KatjaNeural` | `pl` | `pl-PL-ZofiaNeural` |
| `it` | `it-IT-ElsaNeural` | `ru` | `ru-RU-SvetlanaNeural` |
| `pt` | `pt-BR-FranciscaNeural` | `uk` | `uk-UA-PolinaNeural` |
| `ca` | `ca-ES-JoanaNeural` | `cs` | `cs-CZ-VlastaNeural` |
| `gl` | `gl-ES-RoiNeural` | `sk` | `sk-SK-LukasNeural` |
| `hu` | `hu-HU-TamasNeural` | `ro` | `ro-RO-EmilNeural` |
| `bg` | `bg-BG-KalinaNeural` | `el` | `el-GR-NestorasNeural` |
| `sv` | `sv-SE-SofieNeural` | `tr` | `tr-TR-AhmetNeural` |
| `nb` | `nb-NO-PernilleNeural` | `ar` | `ar-EG-ShakirNeural` |
| `ja` | `ja-JP-NanamiNeural` | `he` | `he-IL-HilaNeural` |
| `ko` | `ko-KR-SunHiNeural` | `hi` | `hi-IN-SwaraNeural` |
| `zh` | `zh-CN-XiaoxiaoNeural` | `th` | `th-TH-PremwadeeNeural` |
| `vi` | `vi-VN-HoaiMyNeural` | `id` | `id-ID-GadisNeural` |
| `ms` | `ms-MY-YasminNeural` | | |

Para un idioma **sin** voz curada el servidor sigue funcionando: busca
automáticamente cualquier voz disponible de ese idioma entre las 322. Por
ejemplo, para `sw` (suajili) elige `sw-KE-RafikiNeural`.

Y si pides una variante regional concreta, se respeta:

| Pides | Obtienes |
| --- | --- |
| `en-GB` | `en-GB-LibbyNeural` |
| `en-AU` | `en-AU-WilliamMultilingualNeural` |
| `pt-PT` | `pt-PT-DuarteNeural` |
| `es-MX` | `es-MX-DaliaNeural` |
| `zh-TW` | `zh-TW-HsiaoChenNeural` |
| `fr-CA` | `fr-CA-ThierryNeural` |

El motor `sapi5` solo puede usar las voces instaladas en el sistema, así que
su cobertura de idiomas es la que traiga tu sistema operativo. Windows viene
con español e inglés; el resto requiere añadir voces.

## Voces

### Voces neuronales (motor edge)

Catálogo completo de las 322 voces:

```sh
python -m edge_tts --list-voices
```

Voces de español disponibles (45 en total):

| Voz | Acento |
| --- | --- |
| `es-ES-AlvaroNeural` | España, masculina |
| `es-ES-ElviraNeural` | España, femenina |
| `es-ES-XimenaNeural` | España, femenina |
| `es-MX-DaliaNeural` | México, femenina |
| `es-MX-JorgeNeural` | México, masculina |
| `es-US-PalomaNeural` | Estados Unidos, femenina |

### Voces nativas de Windows (motor sapi5)

De serie solo se ven tres voces muy básicas. Windows trae instaladas voces
mejoradas, como **Microsoft Laura** y **Microsoft Pablo**, pero las registra
bajo la rama `Speech_OneCore` del registro, que SAPI5 no lee.

`registrar_voces_onecore.ps1` copia esas claves a la rama que SAPI5 sí consulta.
Ejecútalo **una sola vez** desde PowerShell como administrador:

```sh
powershell -Command "Start-Process powershell -Verb RunAs -ArgumentList '-ExecutionPolicy Bypass -File .\registrar_voces_onecore.ps1'"
```

Es una copia de solo lectura: no borra ni sobrescribe ninguna voz existente, y
omite las claves que ya estén presentes. Después reinicia el cliente MCP y
llama a `listar_voces` para verlas. A partir de ahí quedan disponibles offline
como `Laura` y `Pablo`.

## Integracion en clientes MCP

### OpenCode

Añádelo globalmente para usarlo en todos tus proyectos:

```sh
opencode mcp add voice-summary -- "C:\ruta\mcp-voice-summary\.venv\Scripts\python.exe" "C:\ruta\mcp-voice-summary\server.py"
```

Comprueba la conexión con `opencode mcp list`.

Para fijar motor y voz, edita `~/.config/opencode/opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "voice-summary": {
        "type": "local",
        "command": [
          "C:\\ruta\\mcp-voice-summary\\.venv\\Scripts\\python.exe",
          "C:\\ruta\\mcp-voice-summary\\server.py"
        ],
        "environment": {
          "VOICE_ENGINE": "edge",
          "VOICE_NAME": "es-ES-AlvaroNeural"
        }
      }
    }
  }
}
```

### Claude Desktop

`claude_desktop_config.json`, en `%APPDATA%\Claude\`:

```json
{
  "mcpServers": {
    "voice-summary": {
      "command": "C:\\ruta\\mcp-voice-summary\\.venv\\Scripts\\python.exe",
      "args": ["C:\\ruta\\mcp-voice-summary\\server.py"],
      "env": {
        "VOICE_ENGINE": "edge",
        "VOICE_NAME": "es-ES-AlvaroNeural"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` en el proyecto:

```json
{
  "mcpServers": {
    "voice-summary": {
      "command": "C:\\ruta\\mcp-voice-summary\\.venv\\Scripts\\python.exe",
      "args": ["C:\\ruta\\mcp-voice-summary\\server.py"],
      "env": { "VOICE_ENGINE": "edge", "VOICE_NAME": "es-ES-AlvaroNeural" }
    }
  }
}
```

### Cualquier otro cliente

Es un servidor MCP por stdio estándar, así que basta con declarar el comando,
los argumentos y las variables de entorno.

## Herramientas

### `reproducir_resumen_voz(texto)`

Reproduce el texto en los altavoces del sistema. La longitud es libre: el
asistente envía un resumen corto o desarrollado según la magnitud del trabajo.
Si el texto supera el tope de seguridad, se recorta y la respuesta incluye un
aviso.

### `listar_voces()`

Muestra qué idioma y qué voz están en uso ahora mismo, y las voces instaladas
en el sistema.

### `establecer_idioma(idioma)`

Fija el idioma de la voz sin editar la configuración. Acepta un código ISO 639-1
(`es`, `en`, `fr`), una variante regional (`pt-BR`, `en-GB`), `auto` para
detectar el idioma de cada resumen, o una cadena vacía para volver al idioma
del sistema.

## Controlar la longitud del resumen

La longitud **no la impone el servidor**: la decide el asistente en función de
cuánto ha hecho. Hay dos niveles de control, y conviene entender la diferencia.

### 1. La longitud que elige el asistente (recomendado)

El asistente decide el tamaño según la tarea. Esto se consigue con la instrucción
que le das a tu asistente, y es lo que conviene usar la mayor parte del tiempo,
porque se adapta solo al trabajo.

Un ejemplo de instrucción equilibrada:

```markdown
Al llamar a `reproducir_resumen_voz`, ajusta la longitud del resumen a la
magnitud del trabajo realizado:

- Cambio puntual o pequeño: una frase corta, de 10 a 20 palabras.
- Tarea media o varios archivos: dos o tres frases, de 30 a 60 palabras.
- Tarea grande o proyecto largo: un resumen de 80 a 150 palabras que repase
  las principales acciones realizadas.

Escribe siempre en primera persona, sin narrar código literal.
```

Si prefieres un tono más conversacional, sube los números. Si lo prefieres
conciso, bájalos. No hay un valor correcto único: depende de la duración de las
tareas con las que trabajas y de si lees también la respuesta completa.

### 2. El tope de seguridad del servidor

`MAX_SUMMARY_WORDS` no es una recomendación de longitud, sino un **freno de
emergencia**. Sirve para que un `texto` desmedido no provoque una locución de
varios minutos. Si se supera, el servidor recorta el resumen y lo avisa en la
respuesta.

```json
"environment": { "MAX_SUMMARY_WORDS": "400" }
```

| Valor | Aproximación | Cuándo usarlo |
| --- | --- | --- |
| `0` | Sin recorte | Solo si quieres permitir locuciones ilimitadas |
| `150` | ~1 minuto | Prefieres resúmenes cortos incluso en tareas grandes |
| `400` | ~2-3 minutos | Valor por defecto, equilibrado |
| `800` | ~5 minutos | Trabajos muy largos y no te molesta esperar |

Una voz neuronal en español habla aproximadamente **2,5 palabras por segundo**,
así que 100 palabras son unos 40 segundos. Las frases cortas y las pausas
importan más para la comprensión que el recuento exacto.

Si necesitas más detalle del que permite un resumen largo, la mejor opción es
**dividir la tarea en varias llamadas** en lugar de subir el tope: así el
usuario oye cada fase en el momento en que ocurre, en lugar de un bloque largo
al final.

### 3. Ajustar la velocidad de lectura

Si el resumen te parece demasiado lento, ajusta la velocidad en lugar de la
longitud:

```json
"environment": { "VOICE_RATE": "120" }  // 20% más rápido
```

## Integrar la regla de comportamiento

Para que el asistente la use de forma automática, añade esta instrucción a las
reglas de tu cliente. En OpenCode va en `~/.config/opencode/AGENTS.md`:

```markdown
## Regla de accesibilidad de voz

Tienes disponible la herramienta MCP `voice-summary` con la función
`reproducir_resumen_voz`. Es OBLIGATORIO usarla inmediatamente después de
terminar de modificar código, crear archivos o ejecutar comandos.

Al llamarla, ajusta la longitud del resumen a la magnitud del trabajo:

- Cambio puntual o pequeño: una frase corta, de 10 a 20 palabras.
- Tarea media o varios archivos: dos o tres frases, de 30 a 60 palabras.
- Tarea grande o proyecto largo: un resumen de 80 a 150 palabras que repase
  las principales acciones realizadas.

Escribe en primera persona, sin narrar código literal.
```

## Notas de implementacion

Detalles que no son evidentes y que conviene conocer si vas a modificarlo:

- **`pyttsx3` se bloquea indefinidamente si el engine se crea en un hilo y se
  usa en otro.** COM es *apartment-threaded*. Por eso el motor se inicializa de
  forma perezosa y se usa siempre desde el mismo hilo trabajador.
- Un único hilo trabajador consume una `queue`, de modo que dos locuciones
  consecutivas nunca se pisan ni se cortan.
- `runAndWait` de SAPI5 puede retornar antes de que termine la locución, así que
  hay una espera activa con `isBusy()`. El motor edge usa MCI con espera
  bloqueante, por lo que su timing es exacto.
- El hilo trabajador es *daemon*: la voz nunca impide cerrar el proceso.
- Cualquier fallo de audio se captura y se registra en el log. El servidor MCP
  nunca se cae por no poder hablar.
- Los imports de `pyttsx3`, `edge_tts` y `langdetect` son perezosos, para que el
  servidor arranque aunque falte alguno de ellos.
- La selección de voz por idioma tiene tres niveles de reserva: voz curada del
  idioma, luego cualquier voz de su variante regional preferida, luego
  cualquier voz de ese idioma. Por eso un idioma sin voz curada sigue
  funcionando.
- La reproducción del MP3 es una capa aparte: MCI en Windows y un reproductor
  externo en Linux y macOS, con lista de candidatos y `VOICE_PLAYER` como
  Override manual.

## Problemas frecuentes

**No se oye nada.** Comprueba que el volumen del sistema está activo y que la
salida por defecto es correcta. En el motor `edge`, verifica que hay
conexión a internet.

**`No se encontro ningun reproductor de audio`.** Solo afecta a Linux y macOS
con el motor `edge`. Instala `ffmpeg`, `mpg123` o `vlc`, o define
`VOICE_PLAYER` con tu reproductor. En Windows no ocurre, porque se usa MCI.

**En Linux el motor `sapi5` no encuentra voces.** Instala el sintetizador del
sistema: `sudo apt install espeak-ng libespeak-ng1`. Ten en cuenta que las
voces de espeak son muy inferiores a las de edge-tts.

**La voz suena entrecortada o se pisan mensajes.** Comprueba que solo hay una
instancia del servidor MCP corriendo.

**`ImportError: No module named mcp.server.fastmcp`.** Instala `mcp>=2.0`. En la
versión 2 `FastMCP` pasó a llamarse `MCPServer`. Si necesitas el API antiguo,
fija `mcp<2`.

**`No se pudo reproducir el resumen` en el log.** El mensaje concreto aparece
en el log del servidor. Las causas típicas son voz inexistente
(`VOICE_NAME` mal escrito) o ausencia de red con el motor `edge`.

**Habla en un idioma que no es el del sistema.** Si el texto se reproduce con
acento extraño, casi siempre es porque `VOICE_LANGUAGE=auto` ha detectado mal
el idioma. Es un problema conocido de la detección con textos cortos: fíjalo
con `establecer_idioma("fr")` o con `VOICE_LANGUAGE`. Consulta la tabla de
[fallos de detección](#detección-automática-del-idioma).

## Licencia

MIT. Ver [LICENSE](LICENSE).