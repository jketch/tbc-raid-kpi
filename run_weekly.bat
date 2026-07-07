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

:: Guard: if a CLOUD run (Actions "Weekly pipeline") pushed state we haven't pulled,
:: running locally now would compute trends against a stale prior week. The check is
:: offline-safe (no data branch / no network => proceeds silently).
python scripts\tools\sync_state.py check
if %ERRORLEVEL% EQU 0 goto :sync_ok
echo.
set /p SYNC_CONT="Continue WITHOUT pulling (trends may be wrong)? [y/N]: "
if /i "%SYNC_CONT%"=="y" goto :sync_ok
echo.
echo Run:   python scripts\tools\sync_state.py pull
echo then start this script again.
pause
exit /b 1
:sync_ok

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
    powershell -NoProfile -Command "python scripts\publish.py 2>&1 | Tee-Object -FilePath '%LOGFILE%' -Append; exit $LASTEXITCODE"
    if ERRORLEVEL 1 (
        echo.
        echo WARNING: the deploy did NOT go live -- a guard blocked it or Netlify failed.
        echo See the reason above ^(or run.log^). The LOCAL dashboard is still fine.
    ) else (
        echo.
        echo Done!
    )
    echo Opening dashboard...
    start "" "dashboard\raid_kpi_dashboard.html"
) else (
    echo.
    echo Something went wrong -- full output saved to run.log
)

echo.
echo (log saved to run.log)
echo.
pause
