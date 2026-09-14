"""Setup detecta requisitos nuevos y repara imports rotos sin cambiar versiones."""

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from tools import check_portable as portable


def test_detecta_requisito_agregado_y_version_incompatible(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("numpy>=2\nPillow>=10\n", encoding="utf-8")
    with patch.object(portable.metadata, "version", side_effect=["1.26", PackageNotFoundError()]):
        assert portable.missing_versions(portable.requirements(path)) == ["numpy>=2", "Pillow>=10"]


def test_repara_archivos_faltantes_aunque_pip_crea_que_esta_instalado(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("numpy>=1.26\n", encoding="utf-8")
    with patch.object(portable, "pip_run", return_value=0) as pip, \
         patch.object(portable, "broken_imports", return_value=["numpy"]), \
         patch.object(portable.metadata, "version", return_value="1.26.4"), \
         patch.object(portable, "check", return_value=0):
        assert portable.repair(path) == 0
    assert "--force-reinstall" in pip.call_args_list[1].args
    assert "--no-deps" in pip.call_args_list[1].args
    assert "numpy==1.26.4" in pip.call_args_list[1].args


def test_check_no_instala_nada(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text("numpy>=1.26\n", encoding="utf-8")
    with patch.object(portable.metadata, "version", return_value="1.26.4"), \
         patch.object(portable, "broken_imports", return_value=[]), \
         patch.object(portable, "pip_run", return_value=0) as pip:
        assert portable.check(path) == 0
    pip.assert_called_once_with("check")


def test_fallo_de_pip_no_se_anuncia_como_reparacion_completa(tmp_path):
    with patch.object(portable, "pip_run", return_value=1):
        assert portable.repair(tmp_path / "requirements.txt") == 1


def test_setup_reconoce_modelo_incompleto_y_limita_la_reparacion(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import pytest

    if os.name != "nt":
        pytest.skip("El setup portable es de Windows")
    script = tmp_path / "modelos.ps1"
    # Ejecuta las funciones reales extraidas por el parser de PowerShell,
    # con una carpeta portable desechable. No toca el entorno instalado.
    script.write_text('''param($Source, $Scratch)
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$null, [ref]$null)
$ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$Portable = Join-Path $Scratch 'portable'
$ModelsDir = Join-Path $Portable 'paddlex\\official_models'
$PaddleModels = @('PP-OCRv6_medium_det', 'PP-OCRv5_mobile_rec', 'PP-OCRv6_medium_rec')
foreach ($name in $PaddleModels) {
    $folder = Join-Path $ModelsDir $name
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $folder 'inference.pdiparams') -Value 'weights'
}
if (Test-Modelos) { throw 'Acepto pesos sin configuracion' }
foreach ($name in $PaddleModels) {
    foreach ($file in @('inference.json', 'inference.yml')) {
        Set-Content -LiteralPath (Join-Path $ModelsDir "$name\\$file") -Value 'config'
    }
}
if (-not (Test-Modelos)) { throw 'Rechazo modelos completos' }
[System.IO.File]::WriteAllBytes((Join-Path $ModelsDir 'PP-OCRv6_medium_rec\\inference.yml'), [byte[]]@())
if (Test-Modelos) { throw 'Acepto el modelo VOID incompleto' }
$outside = Join-Path $Scratch 'conservar'
New-Item -ItemType Directory -Path $outside | Out-Null
$rejected = $false
try { Remove-CarpetaPortable $outside } catch { $rejected = $true }
if (-not $rejected -or -not (Test-Path $outside)) { throw 'No protegio la ruta exterior' }
Remove-CarpetaPortable (Join-Path $ModelsDir 'PP-OCRv6_medium_rec')
if (Test-Path (Join-Path $ModelsDir 'PP-OCRv6_medium_rec')) { throw 'No retiro el modelo incompleto' }
if (-not (Test-Modelo 'PP-OCRv5_mobile_rec')) { throw 'Modifico un modelo completo' }
''', encoding="utf-8")
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         str(Path("setup.ps1").resolve()), str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
