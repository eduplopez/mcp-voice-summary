#Requires -RunAsAdministrator
<#
    Registra las voces neuronales de Windows (Microsoft OneCore) en SAPI5.

    Windows instala voces buenas como Microsoft Laura y Microsoft Pablo, pero
    las registra bajo HKLM\SOFTWARE\Microsoft\Speech_OneCore, una rama que
    pyttsx3 y SAPI5 no leen. Este script copia esas claves a la rama que SAPI5
    si consulta, para que aparezcan como voces seleccionables.

    Es una copia de solo lectura del registro: no borra ni modifica voces
    existentes. Para revertirlo, ejecuta el mismo script con -Desregistrar.
#>

[CmdletBinding()]
param(
    [string]$Destino = "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens",
    [string]$Origen = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
)

$ErrorActionPreference = "Stop"

$destino = $Destino -replace '^HKLM:', 'HKLM:\'
$origen = $Origen -replace '^HKLM:', 'HKLM:\'

if (-not (Test-Path $origen)) {
    Write-Host "ERROR: no existe la rama de origen $origen" -ForegroundColor Red
    exit 1
}

if ($PSCmdlet.ShouldProcess($destino, "Copiar claves de voz OneCore")) {
    Write-Host "Origen:  $origen" -ForegroundColor Cyan
    Write-Host "Destino: $destino" -ForegroundColor Cyan
    Write-Host ""

    $copiadas = 0
    $omitidas = 0

    foreach ($token in Get-ChildItem $origen) {
        $nombre = $token.PSChildName

        if (Test-Path (Join-Path $destino $nombre)) {
            Write-Host "  [omitida] $nombre ya existe en SAPI5" -ForegroundColor DarkGray
            $omitidas++
            continue
        }

        # Copia la subclave completa, incluyendo Suspects, Attributes y el valor
        # (default) con el nombre legible de la voz.
        Copy-Item -Path $token.PSPath -Destination $destino -Recurse -Force
        Write-Host "  [copiada] $nombre" -ForegroundColor Green
        $copiadas++
    }

    Write-Host ""
    Write-Host "Voces copiadas: $copiadas | omitidas por duplicado: $omitidas" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Las nuevas voces ya son visibles para SAPI5." -ForegroundColor Green
    Write-Host "Para verlas desde el servidor MCP, reinicia OpenCode y ejecuta listar_voces." -ForegroundColor Green
    Write-Host ""
    Write-Host "Voces anadidas en espanol esperadas: MSTTS_V110_esES_LauraM, MSTTS_V110_esES_PabloM" -ForegroundColor DarkGray
}