@echo off
set PYTHONUTF8=1
REM start_synergy.bat — Inicia el Synergy Router Proxy para pi
REM
REM Una vez ejecutado, pi puede usar el modelo "synergy/auto" que
REM internamente enruta entre Groq, Cerebras, Gemini Flash, NVIDIA y Modal
REM con failover automatico, validacion y correccion.

echo ================================================
echo   Synergy Router Proxy - Portal Pi
echo ================================================
echo.
echo   Este proxy expone una API OpenAI-compatible en
echo   http://127.0.0.1:8788/v1 que pi puede usar como modelo.
echo.
echo   Proveedores configurados:
echo     - Groq (rapido, tier 1)
echo     - Cerebras (rapido, tier 1)
echo     - Gemini Flash (medium, tier 2)
echo     - NVIDIA (medium, tier 2)
echo     - Modal (lento pero grande, tier 3)
echo.
echo   Web Search: DuckDuckGo (automatico)
echo   Failover: automatico con circuit breaker
echo   Validacion: is_non_empty por defecto
echo.
echo   Para usarlo en pi:
echo     1. Ejecuta este script
echo     2. En pi, selecciona el modelo "synergy/auto"
echo        (con /model o --model synergy/auto)
echo ================================================
echo.

cd /d "%~dp0"

python synergy_proxy.py --host 127.0.0.1 --port 8788

pause
