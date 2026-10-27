@echo off
rem Doble clic para elegir o cambiar el entorno de Python (ver setup.ps1).
rem Pasa los mismos parametros: setup.cmd -Check, setup.cmd -Launcher, etc.
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set CODIGO=%ERRORLEVEL%
if not "%CODIGO%"=="0" (
    echo.
    echo El setup termino con errores ^(codigo %CODIGO%^).
)
echo.
pause
exit /b %CODIGO%
