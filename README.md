# Servidor MCP de Resumen por Voz

Un servidor [MCP](https://modelcontextprotocol.io/) local que lee en voz alta un
resumen breve de las acciones que un asistente de IA acaba de realizar en el
código. Pensado como capa de accesibilidad: el usuario oye qué ha hecho el
agente sin necesidad de leer la respuesta completa.

Funciona con dos motores de síntesis, incluido un motor offline.

## Que hace

Expone una herramienta MCP, `reproducir_resumen_voz`, que el asistente invoca
tras modificar código, crear archivos o ejecutar comandos, pasando un resumen
de 10 a 15 palabras en primera persona.

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

## Voces

### Voces neuronales (motor edge)

45 voces en español disponibles. Recomendadas:

| Voz | Acento |
| --- | --- |
| `es-ES-AlvaroNeural` | España, masculina |
| `es-ES-ElviraNeural` | España, femenina |
| `es-ES-XimenaNeural` | España, femenina |
| `es-MX-DaliaNeural` | México, femenina |
| `es-MX-JorgeNeural` | México, masculina |
| `es-US-PalomaNeural` | Estados Unidos, femenina |

Catálogo completo, sin instalar nada más:

```sh
.venv\Scripts\python.exe -m edge_tts --list-voices
```

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

Reproduce el texto en los altavoces del sistema. Pensada para resúmenes de 10
a 15 palabras en primera persona.

### `listar_voces()`

Muestra las voces disponibles y cuál está activa. Útil para elegir
`VOICE_NAME`.

## Integrar la regla de comportamiento

Para que el asistente la use de forma automática, añade esta instrucción a las
reglas de tu cliente. En OpenCode va en `~/.config/opencode/AGENTS.md`:

```markdown
## Regla de accesibilidad de voz

Tienes disponible la herramienta MCP `voice-summary` con la función
`reproducir_resumen_voz`. Es OBLIGATORIO usarla inmediatamente después de
terminar de modificar código, crear archivos o ejecutar comandos.

Al llamarla, pásale un resumen extremadamente conciso (máximo 10 a 15 palabras)
en primera persona sobre lo que acabas de hacer, sin narrar código literal.
Ejemplo: "He actualizado el endpoint de login y corregido las dependencias".
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
- Los imports de `pyttsx3` y `edge_tts` son perezosos, para que el servidor
  arranque aunque falte uno de los dos motores.
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

## Licencia

MIT. Ver [LICENSE](LICENSE).