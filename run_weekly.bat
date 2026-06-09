@echo off
:: ═══════════════════════════════════════════════════════════
:: CWF Raid Dashboard — Weekly Update Script
:: ═══════════════════════════════════════════════════════════
::
:: HOW TO USE EACH WEEK:
::   1. Copy your WoWCombatLog.txt into the  logs\  folder
::   2. Double-click this file (or run it in cmd)
::   3. Enter the WCL report code when prompted
::      (from: fresh.warcraftlogs.com/reports/XXXXXX)
::   4. Dashboard opens automatically when done
::
:: Full output is always saved to run.log in this folder.
:: The script auto-picks the newest .txt in logs\
:: ═══════════════════════════════════════════════════════════

cd /d "%~dp0"

set "LOGFILE=%~dp0run.log"

(
    echo ============================================================
    echo  Run: %DATE% %TIME%
    echo ============================================================
) > "%LOGFILE%"

set /p REPORT_CODE="Enter WCL report code (e.g. PqynTVBF67pN3Gtg): "

if "%REPORT_CODE%"=="" (
    echo No report code entered. Exiting.
    pause
    exit /b 1
)

echo.
echo Running dashboard update... (logging to run.log)
echo   Report : %REPORT_CODE%
echo   Log    : newest file in logs\
echo   Output : dashboard\raid_kpi_dashboard.html
echo.

:: Run pipeline via PowerShell so Tee-Object streams to console + log simultaneously.
:: PowerShell exits with $LASTEXITCODE so ERRORLEVEL is preserved.
powershell -NoProfile -Command "python scripts\wcl_auto_dashboard.py %REPORT_CODE% 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append; exit $LASTEXITCODE"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Publishing dashboard to Netlify...
    powershell -NoProfile -Command "python scripts\publish.py 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append"
    echo.
    echo Done! Opening dashboard...
    start "" "dashboard\raid_kpi_dashboard.html"
) else (
    echo.
    echo Something went wrong -- full output saved to run.log
)

echo.
echo (log saved to run.log)
echo.
pause
