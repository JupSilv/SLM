@echo off
cd /d "%~dp0"
set "BOT_DIR=%~dp0"
set "BOT_PYTHON=py.exe"

if exist "venv\Scripts\python.exe" (
    set "BOT_PYTHON=%~dp0venv\Scripts\python.exe"
)

:LOOP
echo ===================================================
echo [%date% %time%] Iniciando o Deskubuddy SLM...
echo ===================================================

:: Mantem o bot ativo por 48 minutos; se ainda estiver rodando, encerra
:: somente sua arvore de processos e inicia um novo ciclo sem pausa.
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Start-Process -FilePath $env:BOT_PYTHON -ArgumentList 'main.py' -WorkingDirectory $env:BOT_DIR -NoNewWindow -PassThru -ErrorAction Stop; if (-not $p.WaitForExit(2880000)) { Write-Host 'Limite de 48 minutos atingido; reiniciando o bot...'; & taskkill.exe /PID $p.Id /T /F; if ($LASTEXITCODE -ne 0) { exit 1 }; $p.WaitForExit() } else { exit $p.ExitCode } } catch { Write-Error ('Falha ao iniciar ou supervisionar o Python: ' + $_.Exception.Message); exit 1 }"

if errorlevel 1 (
    echo Falha ao iniciar ou supervisionar o bot. Nova tentativa em 5 segundos...
    timeout /t 5 /nobreak >nul
)

goto LOOP