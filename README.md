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

- Windows 10 u 11. El motor `sapi5` depende del sintetizador nativo de Windows
  y el reproductor de audio usa MCI, que es específico de Windows.
- Python 3.10 o superior.

## Instalacion

```sh
git clone https://github.com/TU-USUARIO/mcp-voice-summary.git
cd mcp-voice-summary

python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

Comprueba que arranca:

```sh
.venv\Scripts\python.exe server.py
```

El servidor habla por stdio, asi que no veras nada en la consola. Eso es
correcto: cualquier texto que imprima en stdout romperia el protocolo. Ctrl+C
para salir.

## Configuracion

Todo se controla por variables de entorno.

| Variable | Valores | Por defecto | Descripcion |
| --- | --- | --- | --- |
| `VOICE_ENGINE` | `edge` o `sapi5` | `sapi5` | Motor de síntesis |
| `VOICE_NAME` | id o nombre de voz | ver abajo | Voz concreta |
| `VOICE_RATE` | `100`, `110`, `-15%` | `100` | Velocidad |
| `VOICE_VOLUME` | `100` | `100` | Volumen |

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

`pyttsx3` sobre el sintetizador nativo de Windows. **Offline y sin latencia**,
pero las voces disponibles son de calidad básica.

```json
"environment": {
  "VOICE_ENGINE": "sapi5",
  "VOICE_NAME": "es-es"
}
```

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

## Problemas frecuentes

**No se oye nada.** Comprueba que el volumen del sistema está activo y que la
salida por defecto es correcta. En el motor `edge`, verifica que hay
conexión a internet.

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