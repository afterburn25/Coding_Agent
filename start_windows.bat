@echo off
setlocal
cd /d "%~dp0"
if not exist config.json copy /Y config.example.json config.json >nul
set "WORKSPACE=%~1"
if "%WORKSPACE%"=="" set "WORKSPACE=%CD%"
echo Starting Local Code Agent...
echo Workspace: %WORKSPACE%
echo Open http://127.0.0.1:8765 in your browser.
python -m localcodeagent --workspace "%WORKSPACE%" --config config.json
endlocal
