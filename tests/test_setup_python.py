"""El setup alterna entornos locales y valida descargas sin tocar el sistema."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import launcher_gui


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_functions(tmp_path):
    if os.name != "nt":
        pytest.skip("El setup es de Windows")

    def run(body, *args):
        script = tmp_path / "prueba setup.ps1"
        script.write_text(
            r'''param($Source, $Root, $BasePython, $Extra)
$ErrorActionPreference = 'Stop'
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$Portable = Join-Path $Root 'portable'
$ModeFile = Join-Path $Portable 'python-mode.txt'
$PythonVersion = '3.12.10'
$PythonMode = 'portable'
$UseSystemPython = $false
$UsePortablePython = $false
$PythonPath = ''
$Check = $false
$Force = $false
''' + body,
            encoding="utf-8",
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             str(ROOT / "setup.ps1"), str(tmp_path), sys.executable, *args],
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                     TEMP=str(tmp_path), TMP=str(tmp_path)),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    return run


def test_menu_instala_y_cambia_ambos_modos_sin_borrar_el_anterior(setup_functions):
    output = setup_functions(r'''
$script:answers = [System.Collections.Generic.Queue[string]]::new()
foreach ($answer in @('x', '2', '', '1')) { $script:answers.Enqueue($answer) }
function Read-Host { param($Prompt) return $script:answers.Dequeue() }
Select-Python
if ($PythonMode -ne 'system' -or $PythonExe -ne (Join-Path $Portable 'venv\Scripts\python.exe')) { throw 'No eligio el venv' }
if (Test-Path $ModeFile) { throw 'Guardo la eleccion antes de completar el setup' }
Save-PythonMode
Select-Python
if ($PythonMode -ne 'system') { throw 'Enter no conservo el modo' }
New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null
Set-Content (Join-Path $PythonDir 'conservar.txt') 'entorno anterior'
Select-Python
if ($PythonMode -ne 'portable' -or $PythonExe -ne (Join-Path $Portable 'python312\tools\python.exe')) { throw 'No volvio al portable' }
if ((Get-Content $ModeFile).Trim() -ne 'system') { throw 'Un cambio pendiente altero el modo activo' }
Save-PythonMode
if ((Get-Content $ModeFile).Trim() -ne 'portable') { throw 'No guardo el cambio' }
if (-not (Test-Path (Join-Path $Portable 'venv\conservar.txt'))) { throw 'Borro el otro entorno' }
if ($script:answers.Count) { throw 'No mostro el menu en cada instalacion' }
''')
    assert "Modo actual: Python de la PC con venv" in output
    assert "1. Python portable" in output
    assert "2. Python de la PC con venv" in output


def test_check_y_parametros_explicitos_no_preguntan_ni_guardan(setup_functions):
    setup_functions(r'''
function Read-Host { throw 'No debe preguntar' }
$Check = $true
Select-Python
if (Test-Path $Portable) { throw 'Check creo carpetas' }
$Check = $false
$UseSystemPython = $true
Select-Python
if ($PythonMode -ne 'system') { throw 'Ignoro el modo explicito' }
$UseSystemPython = $false
$UsePortablePython = $true
Select-Python
if ($PythonMode -ne 'portable') { throw 'Ignoro el modo portable' }
$PythonPath = $BasePython
$rejected = $false
try { Select-Python } catch { $rejected = $true }
if (-not $rejected) { throw 'Acepto dos modos incompatibles' }
if (Test-Path $ModeFile) { throw 'Guardo sin instalar' }
''')


def test_crea_venv_real_aislado_con_pip_y_comprueba_dependencias(setup_functions, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    shutil.copyfile(ROOT / "tools" / "check_portable.py", tools / "check_portable.py")
    # ensurepip de 3.12 incluye pip, pero no setuptools. Se prueba con una
    # rueda local minima para que tampoco haga falta descargar un backend.
    import zipfile

    wheel = tmp_path / "bits_setup_prueba-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("bits_prueba.py", "VALOR = 42\n")
        archive.writestr("bits_setup_prueba-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: bits-setup-prueba\nVersion: 1.0\n")
        archive.writestr("bits_setup_prueba-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: BITS\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("bits_setup_prueba-1.0.dist-info/RECORD", "")
    # El verificador de requirements acepta requisitos PEP 508, no rutas solas.
    (tmp_path / "requirements.txt").write_text(f"bits-setup-prueba @ {wheel.as_uri()}\n", encoding="utf-8")
    setup_functions(r'''
$PythonPath = $BasePython
Select-Python
Install-Python
if (-not (Test-PythonPortable)) { throw 'No valido el venv' }
$env:PIP_CACHE_DIR = Join-Path $Root 'pip-cache'
Install-Dependencias
& $PythonExe -I -c "import sys, bits_prueba; from pathlib import Path; assert sys.prefix != sys.base_prefix; assert bits_prueba.VALOR == 42; assert Path(bits_prueba.__file__).is_relative_to(sys.prefix)"
if ($LASTEXITCODE) { throw 'No instalo las dependencias dentro del venv' }
& $BasePython -I -c "import importlib.util; assert importlib.util.find_spec('bits_prueba') is None"
if ($LASTEXITCODE) { throw 'Instalo un paquete en el Python base' }
# Repetir sin una ruta explicita reutiliza el venv aunque no haya Python en PATH.
$PythonPath = ''
function Find-SystemPython { throw 'No debe buscar una base para un venv sano' }
Install-Python
if (Test-Path $ModeFile) { throw 'Cambio el modo antes de terminar' }
''')


def test_rechaza_python_incompatible_sin_destruir_venv(setup_functions):
    incompatible = shutil.which("python")
    if not incompatible:
        pytest.skip("No hay un segundo Python para probar el rechazo")
    version = subprocess.check_output([incompatible, "-I", "-c", "import sys; print(sys.version_info[:2])"], text=True)
    if "(3, 12)" in version:
        pytest.skip("El segundo Python tambien es 3.12")
    setup_functions(r'''
$PythonPath = $Extra
Select-Python
New-Item -ItemType Directory -Path $PythonDir -Force | Out-Null
$keep = Join-Path $PythonDir 'conservar.txt'
Set-Content $keep 'anterior'
$rejected = $false
try { Install-Python } catch {
    if ($_ -notmatch '3.12') { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'Acepto un Python incompatible' }
if (-not (Test-Path $keep)) { throw 'Borro el venv antes de validar' }
''', incompatible)


def test_descarga_tolera_revocacion_inaccesible_y_exige_sha256(setup_functions, tmp_path):
    payload = b"paquete verificado"
    checksum = hashlib.sha256(payload).hexdigest()
    setup_functions(r'''
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1')
function Get-Command { return [pscustomobject]@{ Source = 'Fake-Curl' } }
function Fake-Curl {
    $arguments = @($args)
    if ('--ssl-revoke-best-effort' -notin $arguments) { throw 'Falta tratar la revocacion inaccesible' }
    if ('--insecure' -in $arguments -or '--ssl-no-revoke' -in $arguments) { throw 'Desactivo la validacion' }
    $destination = $arguments[[Array]::IndexOf($arguments, '--output') + 1]
    [System.IO.File]::WriteAllBytes($destination, [System.Text.Encoding]::UTF8.GetBytes('paquete verificado'))
    $global:LASTEXITCODE = 0
}
$destination = Join-Path $Root 'cache\python.zip'
Get-Descarga -Url 'https://example.invalid/python' -Destino $destination -Sha256 $Extra
if (-not (Test-Path $destination)) { throw 'No publico el paquete verificado' }
Remove-Item -LiteralPath $destination
$rejected = $false
try { Get-Descarga -Url 'https://example.invalid/python' -Destino $destination -Sha256 ('0' * 64) } catch { $rejected = $true }
if (-not $rejected -or (Test-Path $destination) -or (Test-Path "$destination.part")) { throw 'Acepto un paquete sin el hash esperado' }
''', checksum)


@pytest.mark.parametrize("mode", ["portable", "system"])
@pytest.mark.parametrize("console", [False, True])
def test_launcher_usa_el_modo_elegido_aunque_existan_ambos(tmp_path, mode, console):
    portable = tmp_path / "portable"
    for base in (portable / "python312" / "tools", portable / "venv" / "Scripts"):
        base.mkdir(parents=True)
        (base / "python.exe").touch()
        if not console:
            (base / "pythonw.exe").touch()
    (portable / "venv" / "pyvenv.cfg").touch()
    (portable / "python-mode.txt").write_text(mode + "\n", encoding="ascii")
    folder = portable / "venv" / "Scripts" if mode == "system" else portable / "python312" / "tools"
    assert launcher_gui._pythonw(tmp_path) == folder / ("python.exe" if console else "pythonw.exe")


def test_launcher_conserva_portable_anterior_y_no_oculta_un_modo_roto(tmp_path):
    portable = tmp_path / "portable"
    base = portable / "python312" / "tools"
    base.mkdir(parents=True)
    exe = base / "pythonw.exe"
    exe.touch()
    assert launcher_gui._pythonw(tmp_path) == exe
    for mode in ("system", "desconocido"):
        (portable / "python-mode.txt").write_text(mode, encoding="ascii")
        assert launcher_gui._pythonw(tmp_path) is None
