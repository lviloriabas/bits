<#
.SYNOPSIS
    Prepara BITS con Python portable o Python de la PC en un venv aislado,
    instala las dependencias y precarga los modelos OCR.

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

.PARAMETER UseSystemPython
    Usa CPython 3.12 de 64 bits de la PC y crea portable\venv.

.PARAMETER UsePortablePython
    Usa el Python portable incluido, aunque antes se eligiera el de la PC.

.PARAMETER PythonPath
    Ruta de un Python de la PC. Implica UseSystemPython.

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
    [switch]$CleanCache,
    [switch]$UseSystemPython,
    [switch]$UsePortablePython,
    [string]$PythonPath
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
$ModeFile = Join-Path $Portable 'python-mode.txt'
$PythonMode = 'portable'
$PythonRelativeDir = 'portable\python312'
$PythonRelativeExe = 'portable\python312\tools\python.exe'
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

function Test-PythonCompatible {
    param([string]$Ruta, [switch]$Venv)
    if (-not (Test-Path -LiteralPath $Ruta -PathType Leaf)) { return $false }
    $codigo = "import sys, struct, platform; assert sys.implementation.name == 'cpython'; assert sys.version_info[:2] == (3, 12) and sys.version_info >= (3, 12, 10); assert struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64')"
    if ($Venv) {
        # No basta con encontrar un exe: debe ser el venv de esta carpeta y
        # no debe heredar paquetes de la instalacion base.
        $codigo += "; from pathlib import Path; assert sys.prefix != sys.base_prefix; assert Path(sys.prefix).resolve() == Path(sys.executable).resolve().parents[1]; assert 'include-system-site-packages = false' in (Path(sys.prefix) / 'pyvenv.cfg').read_text().lower()"
    }
    return (Invoke-Programa -Ruta $Ruta -Argumentos @('-I', '-c', $codigo))
}

function Test-PythonPortable {
    return (Test-PythonCompatible -Ruta $PythonExe -Venv:($PythonMode -eq 'system'))
}

function Select-Python {
    if ($UsePortablePython -and ($UseSystemPython -or $PythonPath)) {
        throw 'Elija solo un modo de Python: portable o de la PC.'
    }
    $modo = 'portable'
    if (Test-Path -LiteralPath $ModeFile) {
        $guardado = (Get-Content -LiteralPath $ModeFile -Raw).Trim()
        if ($guardado -in @('portable', 'system')) { $modo = $guardado }
    }
    if ($UseSystemPython -or $PythonPath) { $modo = 'system' }
    if ($UsePortablePython) { $modo = 'portable' }

    # Al instalar se pregunta tambien desde setup.ps1. Los parametros de modo
    # permiten automatizar sin preguntas; -Check nunca pide entrada.
    if (-not $Check -and -not ($UseSystemPython -or $UsePortablePython -or $PythonPath)) {
        $actual = if ($modo -eq 'system') { 'Python de la PC con venv' } else { 'Python portable' }
        Write-Host "Modo actual: $actual"
        Write-Host '  1. Python portable (puede copiarse a otra PC)'
        Write-Host '  2. Python de la PC con venv (requiere Python 3.12 de 64 bits)'
        do {
            $opcion = Read-Host 'Elija 1 o 2, o pulse Enter para conservar el modo actual'
        } while ($opcion -notin @('', '1', '2'))
        if ($opcion -eq '1') { $modo = 'portable' }
        if ($opcion -eq '2') { $modo = 'system' }
    }

    $script:PythonMode = $modo
    if ($modo -eq 'system') {
        $script:PythonRelativeDir = 'portable\venv'
        $script:PythonRelativeExe = 'portable\venv\Scripts\python.exe'
    }
    else {
        $script:PythonRelativeDir = 'portable\python312'
        $script:PythonRelativeExe = 'portable\python312\tools\python.exe'
    }
    $script:PythonDir = Join-Path $Root $PythonRelativeDir
    $script:PythonExe = Join-Path $Root $PythonRelativeExe
}

function Find-SystemPython {
    # Una ruta explicita nunca cae silenciosamente en otro interprete.
    if ($PythonPath) {
        if (-not (Test-PythonCompatible $PythonPath)) {
            throw 'El Python indicado no es compatible. Use CPython 3.12.10 o posterior de la serie 3.12, de 64 bits (x64).'
        }
        return (Resolve-Path -LiteralPath $PythonPath).Path
    }
    $candidatos = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $candidatos += @(Get-SalidaPrograma -Ruta $py.Source -Argumentos @(
                '-3.12', '-I', '-c', 'import sys; print(sys._base_executable)')) |
            Where-Object { $_ -is [string] -and (Test-Path -LiteralPath $_ -PathType Leaf) }
    }
    foreach ($nombre in @('python.exe', 'python3.12.exe', 'python3.exe')) {
        $candidatos += @(Get-Command $nombre -All -CommandType Application -ErrorAction SilentlyContinue |
                Where-Object { $_.Source -notlike '*\Microsoft\WindowsApps\*' } |
                ForEach-Object { $_.Source })
    }
    foreach ($base in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if ($base) {
            $candidatos += Join-Path $base 'Programs\Python\Python312\python.exe'
            $candidatos += Join-Path $base 'Python312\python.exe'
        }
    }
    foreach ($ruta in ($candidatos | Select-Object -Unique)) {
        if (Test-PythonCompatible $ruta) { return $ruta }
    }
    throw ('No se encontro CPython 3.12.10 o posterior de la serie 3.12, de 64 bits (x64). ' +
        'Instale esa version o indique su ruta con -PythonPath. Tambien puede elegir Python portable.')
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
        # Schannel puede no alcanzar la lista de revocacion en redes de
        # empresa. Conserva la validacion TLS y rechaza certificados revocados;
        # solo tolera listas ausentes o inaccesibles. SHA256 sigue obligatorio.
        & $curl.Source --ssl-revoke-best-effort --location --fail --retry 3 --continue-at - `
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
    if ($PythonMode -eq 'system') {
        Install-Venv
        return
    }
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

function Install-Venv {
    Write-Paso 'Python de la PC en un entorno virtual aislado'
    if ((Test-PythonPortable) -and -not $Force -and -not $PythonPath) {
        Write-Listo 'ya esta en portable\venv'
        return
    }
    # Validar primero evita borrar un venv sano si se indico una ruta erronea.
    $basePython = Find-SystemPython
    if ((Test-PythonPortable) -and -not $Force) {
        $baseActual = Get-SalidaPrograma -Ruta $PythonExe -Argumentos @(
            '-I', '-c', 'import sys; print(sys._base_executable)')
        if ($baseActual -eq $basePython) {
            Write-Listo 'ya esta en portable\venv con ese Python'
            return
        }
    }
    Write-Detalle 'creando portable\venv sin paquetes globales'
    Remove-CarpetaPortable $PythonDir
    & $basePython -I -m venv --copies $PythonDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-PythonPortable)) {
        throw 'No se pudo crear el entorno virtual de Python 3.12.'
    }
    Write-Listo 'entorno virtual listo en portable\venv'
}

function Save-PythonMode {
    # Se publica la eleccion solo despues de preparar todos los componentes.
    # El launcher anterior sigue disponible si el cambio no se completa.
    New-Item -ItemType Directory -Path $Portable -Force | Out-Null
    $temporal = "$ModeFile.tmp"
    Set-Content -LiteralPath $temporal -Value $PythonMode -Encoding ASCII
    Move-Item -LiteralPath $temporal -Destination $ModeFile -Force
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
            Componente = if ($PythonMode -eq 'system') { 'Python 3.12 (venv de la PC)' } else { "Python $PythonVersion portable" }
            Estado     = if (Test-PythonPortable) { 'ok' } else { 'FALTA' }
            Ruta       = $PythonRelativeDir
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

Write-Host "Preparacion de BITS - Clasificacion de Bitacoras" -ForegroundColor White
Write-Detalle "carpeta: $Root"
Select-Python

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

Save-PythonMode
Show-Estado
Write-Host "Todo listo. Abra BITS.exe o ejecute:" -ForegroundColor Green
Write-Host "    $PythonRelativeExe run_gui.py"
if ($PythonMode -eq 'system') {
    Write-Aviso 'Este venv depende del Python de esta PC. Si mueve la carpeta, vuelva a ejecutar setup.cmd.'
}
