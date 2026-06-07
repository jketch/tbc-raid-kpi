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
:: The script auto-picks the newest .txt in logs\
:: ═══════════════════════════════════════════════════════════

cd /d "%~dp0"

set /p REPORT_CODE="Enter WCL report code (e.g. PqynTVBF67pN3Gtg): "

if "%REPORT_CODE%"=="" (
    echo No report code entered. Exiting.
    pause
    exit /b 1
)

echo.
echo Running dashboard update...
echo   Report : %REPORT_CODE%
echo   Log    : newest file in logs\
echo   Output : dashboard\raid_kpi_dashboard.html
echo.

python scripts\wcl_auto_dashboard.py %REPORT_CODE%

if %ERRORLEVEL% EQU 0 (
    echo.
    echo Done! Opening dashboard...
    start "" "dashboard\raid_kpi_dashboard.html"
    echo.
    pause
) else (
    echo.
    echo Something went wrong. Check the output above.
    pause
)
