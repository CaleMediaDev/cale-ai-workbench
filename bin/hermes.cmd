@echo off
setlocal
set "HERMES_HOME=%~dp0..\data"
set "PYTHONUTF8=1"
set "TEMP=%~dp0..\Temp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
set "NODE_PATH=%~dp0..\Toolbox\node_modules;%APPDATA%\npm\node_modules\@playwright\mcp\node_modules;%NODE_PATH%"
set "PATH=%~dp0..\Toolbox\node-v26.13.0-win-x64;%~dp0..\Toolbox\node_modules\.bin;%~dp0..\Toolbox\python\Scripts;%PATH%"
set "JAVA_HOME=%~dp0..\Toolbox\jdk-21.0.13+9"
set "GHIDRA_INSTALL_DIR=%~dp0..\Toolbox\ghidra_12.1.4_PUBLIC"
for /f "delims=" %%K in ('""%~dp0..\Toolbox\python\Scripts\python.exe" "%~dp0..\cloud_ai.py" --ensure-token"') do set "LOCAL_AI_ROUTER_KEY=%%K"
if not defined LOCAL_AI_ROUTER_KEY exit /b 1
"%~dp0..\data\bin\hermes.exe" %*
exit /b %errorlevel%
