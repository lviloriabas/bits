<#
.SYNOPSIS
    Reconstruye el entorno portable descargando todo lo que el repositorio no
    versiona: Python 3.12, las dependencias y los modelos OCR.

.DESCRIPTION
    El repositorio guarda solo codigo fuente. La carpeta portable\ (unos 2 GB)
    queda fuera de git por peso, asi que en una maquina nueva hay que traerla:

        portable\python312   interprete Python 3.12 + dependencias (pip)
        portable\paddlex     modelos PaddleOCR (detector + reconocedor)

    El script es idempotente: lo que ya esta completo no se vuelve a bajar.
    Necesita internet solo mientras corre; despues la aplicacion trabaja
    offline, como exige el paquete portable.

.PARAMETER Check
    No descarga nada: solo informa que componentes estan presentes.

.PARAMETER Launcher
    Regenera BITS.exe con PyInstaller al terminar.

.PARAMETER Force
    Rehace los componentes aunque ya esten instalados.

.PARAMETER CleanCache
    Borra portable\.cache (instaladores descargados) al terminar.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup.ps1 -Check
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Launcher,
    [switch]$Force,
    [switch]$CleanCache
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# --- Versiones fijas -------------------------------------------------------
# Son exactamente las del portable verificado. PaddlePaddle no publica ruedas
# para Python 3.14, por eso 3.12 (ver README).
$PythonVersion = '3.12.10'
$PythonUrl = "https://globalcdn.nuget.org/packages/python.$PythonVersion.nupkg"
$PythonSha = '0EB85C2DFCCCCF1B17352DE4C397F69194035B7D37149EACC16F1147D93DE3B8'

# Los mismos nombres que fija app/ocr/engine.py y descarga tools\precache_paddle.py.
$PaddleModels = @('PP-OCRv6_medium_det', 'PP-OCRv5_mobile_rec', 'PP-OCRv6_medium_rec')

# --- Rutas -----------------------------------------------------------------
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Portable = Join-Path $Root 'portable'
$Cache = Join-Path $Portable '.cache'
$PythonDir = Join-Path $Portable 'python312'
$PythonExe = Join-Path $PythonDir 'tools\python.exe'
$ModelsDir = Join-Path $Portable 'paddlex\official_models'
$LauncherExe = Join-Path $Root 'BITS.exe'
$env:PADDLE_PDX_CACHE_HOME = Join-Path $Portable 'paddlex'
$env:PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK = '1'
$env:PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT = '0'
$env:FLAGS_use_mkldnn = '0'

# --- Salida ----------------------------------------------------------------

function Write-Paso {
    param([string]$Texto)
    Write-Host ""
    Write-Host "==> $Texto" -ForegroundColor Cyan
}

function Write-Detalle {
    param([string]$Texto)
    Write-Host "    $Texto" -ForegroundColor DarkGray
}

function Write-Listo {
    param([string]$Texto)
    Write-Host "    $Texto" -ForegroundColor Green
}

function Write-Aviso {
    param([string]$Texto)
    Write-Host "    $Texto" -ForegroundColor Yellow
}

# --- Utilidades ------------------------------------------------------------

function Invoke-Programa {
    <#
        Ejecuta un programa externo callando su salida y devuelve si termino
        bien. Baja ErrorActionPreference mientras corre porque, con 'Stop',
        PowerShell 5.1 convierte cualquier linea de stderr en error terminante.
    #>
    param(
        [string]$Ruta,
        [string[]]$Argumentos
    )
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Ruta @Argumentos 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
    finally {
        $ErrorActionPreference = $anterior
    }
}

function Get-SalidaPrograma {
    <#
        Igual que Invoke-Programa pero devuelve las lineas que imprimio el
        programa (stdout y stderr juntos).
    #>
    param(
        [string]$Ruta,
        [string[]]$Argumentos
    )
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        return (& $Ruta @Argumentos 2>&1)
    }
    finally {
        $ErrorActionPreference = $anterior
    }
}

function Test-PythonPortable {
    if (-not (Test-Path $PythonExe)) { return $false }
    return (Invoke-Programa -Ruta $PythonExe -Argumentos @(
            '-c', "import sys; assert sys.version_info[:2] == (3, 12)"))
}

function Test-Dependencias {
    if (-not (Test-Path $PythonExe)) { return $false }
    return (Invoke-Programa -Ruta $PythonExe -Argumentos @(
            (Join-Path $Root 'tools\check_portable.py'), '--check',
            '--requirements', (Join-Path $Root 'requirements.txt')))
}

function Test-Modelo {
    param([string]$Nombre)
    foreach ($archivo in @('inference.json', 'inference.pdiparams', 'inference.yml')) {
        $ruta = Join-Path $ModelsDir "$Nombre\$archivo"
        if (-not (Test-Path -LiteralPath $ruta -PathType Leaf)) { return $false }
        if ((Get-Item -LiteralPath $ruta).Length -eq 0) { return $false }
    }
    return $true
}

function Test-Modelos {
    foreach ($modelo in $PaddleModels) {
        if (-not (Test-Modelo $modelo)) { return $false }
    }
    return $true
}

function Remove-CarpetaPortable {
    param([string]$Ruta)
    $base = [System.IO.Path]::GetFullPath($Portable).TrimEnd('\') + '\'
    $destinoAbsoluto = [System.IO.Path]::GetFullPath($Ruta)
    if (-not $destinoAbsoluto.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Ruta fuera de portable: $destinoAbsoluto"
    }
    if (Test-Path -LiteralPath $destinoAbsoluto) {
        $resuelta = (Resolve-Path -LiteralPath $destinoAbsoluto).Path
        if (-not $resuelta.StartsWith($base, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Ruta resuelta fuera de portable: $resuelta"
        }
        Remove-Item -LiteralPath $resuelta -Recurse -Force
    }
}

function Get-TamanoMB {
    param([string]$Ruta)
    if (-not (Test-Path $Ruta)) { return 0 }
    $bytes = (Get-ChildItem -Path $Ruta -Recurse -File -ErrorAction SilentlyContinue |
        Measure-Object -Property Length -Sum).Sum
    if ($null -eq $bytes) { return 0 }
    return [math]::Round($bytes / 1MB)
}

function Get-Descarga {
    <#
        Descarga con reanudacion y verifica SHA256. El archivo final solo
        aparece cuando el hash coincide, asi que una descarga cortada nunca
        se confunde con un instalador completo.
    #>
    param(
        [string]$Url,
        [string]$Destino,
        [string]$Sha256
    )

    if (Test-Path $Destino) {
        $hash = (Get-FileHash -Path $Destino -Algorithm SHA256).Hash
        if ($hash -eq $Sha256) {
            Write-Detalle "ya descargado: $(Split-Path -Leaf $Destino)"
            return
        }
        Write-Aviso "hash distinto en la copia guardada, se descarga otra vez"
        Remove-Item -LiteralPath $Destino -Force
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $Destino) -Force | Out-Null
    $parcial = "$Destino.part"
    Write-Detalle "bajando $Url"

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & $curl.Source --location --fail --retry 3 --continue-at - `
            --output $parcial $Url
        if ($LASTEXITCODE -ne 0) {
            throw "Fallo la descarga de $Url (curl $LASTEXITCODE)."
        }
    }
    else {
        if (Test-Path $parcial) { Remove-Item -LiteralPath $parcial -Force }
        Invoke-WebRequest -Uri $Url -OutFile $parcial -UseBasicParsing
    }

    $hash = (Get-FileHash -Path $parcial -Algorithm SHA256).Hash
    if ($hash -ne $Sha256) {
        Remove-Item -LiteralPath $parcial -Force
        throw ("El archivo bajado de $Url no coincide con el hash esperado " +
            "($hash != $Sha256). Vuelva a intentarlo.")
    }
    Move-Item -LiteralPath $parcial -Destination $Destino -Force
}

function Find-SevenZip {
    $comando = Get-Command '7z.exe' -ErrorAction SilentlyContinue
    if ($comando) { return $comando.Source }
    $candidatos = @(
        (Join-Path $env:ProgramFiles '7-Zip\7z.exe'),
        (Join-Path ${env:ProgramFiles(x86)} '7-Zip\7z.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\7-Zip\7z.exe')
    )
    foreach ($ruta in $candidatos) {
        if ($ruta -and (Test-Path $ruta)) { return $ruta }
    }
    return $null
}

# --- Componentes -----------------------------------------------------------

function Install-Python {
    Write-Paso "Python $PythonVersion portable"
    if ((Test-PythonPortable) -and -not $Force) {
        Write-Listo "ya esta en portable\python312"
        return
    }
    $paquete = Join-Path $Cache "python.$PythonVersion.nupkg.zip"
    Get-Descarga -Url $PythonUrl -Destino $paquete -Sha256 $PythonSha

    # El paquete NuGet trae el interprete completo bajo tools\, que es la
    # ruta que usan el launcher, el README y los scripts de tools\.
    $temporal = Join-Path $Cache "python-$PythonVersion-extract"
    Remove-CarpetaPortable $temporal
    Write-Detalle 'descomprimiendo'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($paquete, $temporal)

    $destino = Join-Path $PythonDir 'tools'
    Remove-CarpetaPortable $destino
    New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null
    Move-Item -LiteralPath (Join-Path $temporal 'tools') -Destination $destino
    Remove-CarpetaPortable $temporal

    # El paquete NuGet no trae pip; ensurepip lo instala desde la rueda incluida.
    Write-Detalle 'instalando pip'
    & $PythonExe -m ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo instalar pip (ensurepip).' }
    & $PythonExe -m pip install --quiet --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo actualizar pip.' }

    if (-not (Test-PythonPortable)) { throw 'El Python portable no quedo utilizable.' }
    Write-Listo "listo en portable\python312\tools\python.exe"
}

function Install-Dependencias {
    Write-Paso 'Dependencias de Python'
    # Resolver siempre requirements, incluidos extras nuevos. pip conserva
    # los paquetes satisfechos; los imports solos no prueban todos los extras.
    if (-not (Invoke-Programa -Ruta $PythonExe -Argumentos @('-m', 'pip', '--version'))) {
        & $PythonExe -m ensurepip --upgrade --default-pip
        if ($LASTEXITCODE -ne 0) { throw 'No se pudo reparar pip.' }
    }
    Write-Detalle 'comprobando requisitos e instalando o reparando lo faltante'
    $argumentos = @((Join-Path $Root 'tools\check_portable.py'),
        '--requirements', (Join-Path $Root 'requirements.txt'))
    if ($Force) { $argumentos += '--force' }
    & $PythonExe @argumentos
    if ($LASTEXITCODE -ne 0) { throw 'Fallo la instalacion de dependencias.' }
    Write-Listo 'dependencias instaladas'
}

function Install-Modelos {
    Write-Paso 'Modelos PaddleOCR'
    if ((Test-Modelos) -and -not $Force) {
        Write-Listo "$($PaddleModels -join ' + ') ya estan en portable\paddlex"
        return
    }
    foreach ($modelo in $PaddleModels) {
        if ($Force -or -not (Test-Modelo $modelo)) {
            $ruta = Join-Path $ModelsDir $modelo
            Remove-CarpetaPortable $ruta
        }
    }
    Write-Detalle 'tools\precache_paddle.py (descarga y prueba los modelos)'
    & $PythonExe (Join-Path $Root 'tools\precache_paddle.py')
    if ($LASTEXITCODE -ne 0) { throw 'Fallo la precarga de modelos PaddleOCR.' }
    if (-not (Test-Modelos)) {
        throw 'La precarga termino pero faltan pesos en portable\paddlex.'
    }
    Write-Listo 'modelos listos en portable\paddlex'
}

function Build-Launcher {
    Write-Paso 'Launcher BITS.exe'
    Write-Detalle 'instalando pyinstaller'
    & $PythonExe -m pip install --quiet --disable-pip-version-check `
        --no-warn-script-location pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo instalar PyInstaller.' }
    & $PythonExe (Join-Path $Root 'tools\build_launcher.py')
    if ($LASTEXITCODE -ne 0) { throw 'Fallo la construccion del launcher.' }
    Write-Listo 'launcher regenerado'
}

# --- Informe ---------------------------------------------------------------

function Show-Estado {
    $filas = @(
        [pscustomobject]@{
            Componente = "Python $PythonVersion"
            Estado     = if (Test-PythonPortable) { 'ok' } else { 'FALTA' }
            Ruta       = 'portable\python312'
            MB         = Get-TamanoMB $PythonDir
        },
        [pscustomobject]@{
            Componente = 'Dependencias'
            Estado     = if (Test-Dependencias) { 'ok' } else { 'FALTA' }
            Ruta       = 'requirements.txt'
            MB         = 0
        },
        [pscustomobject]@{
            Componente = 'Modelos PaddleOCR'
            Estado     = if (Test-Modelos) { 'ok' } else { 'FALTA' }
            Ruta       = 'portable\paddlex'
            MB         = Get-TamanoMB (Join-Path $Portable 'paddlex')
        },
        [pscustomobject]@{
            Componente = 'Launcher'
            Estado     = if (Test-Path $LauncherExe) { 'ok' } else { 'FALTA' }
            Ruta       = 'BITS.exe'
            MB         = Get-TamanoMB $LauncherExe
        }
    )
    Write-Host ""
    $filas | Format-Table -AutoSize | Out-String | Write-Host
    $pdfs = @(Get-ChildItem -Path (Join-Path $Root 'input') -Filter '*.pdf' `
            -File -ErrorAction SilentlyContinue)
    if ($pdfs.Count -eq 0) {
        Write-Aviso 'input\ no tiene PDFs: los escaneos se copian a mano, no se descargan.'
    }
}

# --- Programa --------------------------------------------------------------

if ($env:OS -ne 'Windows_NT') {
    throw 'El entorno portable es de Windows: ejecute este script en Windows.'
}

Write-Host "Entorno portable de BITS - Clasificacion de Bitacoras" -ForegroundColor White
Write-Detalle "carpeta: $Root"

if ($Check) {
    Show-Estado
    return
}

Install-Python
Install-Dependencias
Install-Modelos
if ($Launcher) { Build-Launcher }

if ($CleanCache -and (Test-Path $Cache)) {
    Remove-CarpetaPortable $Cache
    Write-Detalle 'cache de instaladores borrada'
}

Show-Estado
Write-Host "Todo listo. Abra BITS.exe o ejecute:" -ForegroundColor Green
Write-Host "    portable\python312\tools\python.exe run_cli.py --pdf input\<archivo>.pdf"
