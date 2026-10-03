#Requires -RunAsAdministrator
<#
    Registers Windows neural voices (Microsoft OneCore) with SAPI5.

    Windows ships better voices such as Microsoft Laura and Microsoft Pablo, but
    it registers them under the registry key Speech_OneCore, which pyttsx3 and
    SAPI5 do not read. This script copies those keys into the branch SAPI5 does
    read, so they show up as selectable voices.

    It is a read-only copy: no existing voice is deleted or overwritten. To
    revert, delete the copied keys or restore the registry backup.
#>

[CmdletBinding()]
param(
    [string]$Destination = "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens",
    [string]$Source = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
)

$ErrorActionPreference = "Stop"

$destination = $Destination -replace '^HKLM:', 'HKLM:\'
$source = $Source -replace '^HKLM:', 'HKLM:\'

if (-not (Test-Path $source)) {
    Write-Host "ERROR: source branch $source does not exist" -ForegroundColor Red
    exit 1
}

Write-Host "Source:      $source" -ForegroundColor Cyan
Write-Host "Destination: $destination" -ForegroundColor Cyan
Write-Host ""

$copied = 0
$skipped = 0

foreach ($token in Get-ChildItem $source) {
    $name = $token.PSChildName

    if (Test-Path (Join-Path $destination $name)) {
        Write-Host "  [skipped] $name already exists in SAPI5" -ForegroundColor DarkGray
        $skipped++
        continue
    }

    # Copy the whole subkey, including Suspects, Attributes and the (default)
    # value that holds the readable voice name.
    Copy-Item -Path $token.PSPath -Destination $destination -Recurse -Force
    Write-Host "  [copied]  $name" -ForegroundColor Green
    $copied++
}

Write-Host ""
Write-Host "Copied: $copied | skipped as duplicate: $skipped" -ForegroundColor Cyan
Write-Host ""
Write-Host "The new voices are now visible to SAPI5." -ForegroundColor Green
Write-Host "Restart OpenCode and call list_voices to see them." -ForegroundColor Green
Write-Host ""
Write-Host "Expected Spanish voices: MSTTS_V110_esES_LauraM, MSTTS_V110_esES_PabloM" -ForegroundColor DarkGray