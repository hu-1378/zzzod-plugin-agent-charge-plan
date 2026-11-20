@echo off
chcp 65001 >nul 2>&1
setlocal

rem Charge-plan local web config page launcher.
rem Prefer the repo venv (.venv\Scripts\python.exe); fall back to `uv run python`.
rem
rem Keep this file ASCII-only: cmd.exe parses .bat files with the OEM codepage,
rem so Chinese text in a UTF-8 batch file can split commands apart. All Chinese
rem output comes from server.py, which runs after `chcp 65001`.

set "WEB_DIR=%~dp0"
set "ROOT=%WEB_DIR%..\..\.."

if not exist "%WEB_DIR%server.py" (
    echo [ERROR] server.py was not found next to this bat file.
    pause
    exit /b 1
)

set "PY=%ROOT%\.venv\Scripts\python.exe"
if exist "%PY%" (
    echo [start] using "%PY%"
    echo.
    "%PY%" "%WEB_DIR%server.py" %*
    goto :done
)

where uv >nul 2>&1
if errorlevel 1 goto :no_python

echo [start] .venv\Scripts\python.exe not found, falling back to: uv run python
echo.
pushd "%ROOT%"
uv run python "%WEB_DIR%server.py" %*
popd
goto :done

:no_python
echo [ERROR] Neither .venv\Scripts\python.exe nor uv was found on PATH.
echo         Run "uv sync --group dev" in the repo root first, then retry.
pause
exit /b 1

:done
if errorlevel 1 pause
endlocal