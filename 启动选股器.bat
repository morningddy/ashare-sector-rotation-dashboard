@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title A-Share Auto Picker Launcher
set "HERE=%~dp0"
set "F=%HERE%ashare-auto-picker.html"
set "SRV=%HERE%news-live\server.py"

rem ===========================================================
rem  A-Share Auto Picker - one-click launcher
rem    1) start local relay service on 127.0.0.1:8510 (if needed)
rem    2) open the picker with Edge / Chrome
rem  The picker needs 8510 because embedded windows (preview panel,
rem  webview) block direct calls to quote servers; the relay fetches
rem  the data locally and hands it back.
rem  Usage:
rem    double-click          -> start relay + open picker
rem    launcher.bat news    -> run relay service in foreground only
rem ===========================================================

if /I "%~1"=="news" goto :foreground
if not exist "%F%" goto :notfound

rem ---------- 1) locate a Python that really has akshare ----------
set "PY="
for %%V in (310 311 312 313) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" -c "import akshare, pandas, requests" >nul 2>&1
    if not errorlevel 1 set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
  )
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"

rem ---------- 2) start relay service if port 8510 is free ----------
netstat -ano | findstr /C:":8510" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [relay] already running on 127.0.0.1:8510
  goto :openbrowser
)
if not exist "%SRV%" (
  echo [relay] server.py not found - picker will retry by itself
  goto :openbrowser
)
if not defined PY (
  echo [relay] Python 3.10+ with akshare not found - picker will retry by itself
  goto :openbrowser
)

echo [relay] starting %PY%
start "news-live 127.0.0.1:8510" /min /d "%HERE%news-live" "%PY%" server.py

set /a TRY=0
:waitport
netstat -ano | findstr /C:":8510" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [relay] ready on http://127.0.0.1:8510
  goto :openbrowser
)
set /a TRY+=1
if !TRY! geq 25 (
  echo [relay] not up yet after 25s - picker will keep checking
  goto :openbrowser
)
ping -n 2 127.0.0.1 >nul
goto :waitport

rem ---------- 3) open with a modern browser ----------
:openbrowser
set "PF86=%ProgramFiles(x86)%"
set "PF64=%ProgramFiles%"
set "LA=%LOCALAPPDATA%"
set "B="
if exist "%PF86%\Microsoft\Edge\Application\msedge.exe" set "B=%PF86%\Microsoft\Edge\Application\msedge.exe"
if not defined B if exist "%PF64%\Microsoft\Edge\Application\msedge.exe" set "B=%PF64%\Microsoft\Edge\Application\msedge.exe"
if not defined B if exist "%PF64%\Google\Chrome\Application\chrome.exe" set "B=%PF64%\Google\Chrome\Application\chrome.exe"
if not defined B if exist "%PF86%\Google\Chrome\Application\chrome.exe" set "B=%PF86%\Google\Chrome\Application\chrome.exe"
if not defined B if exist "%LA%\Google\Chrome\Application\chrome.exe" set "B=%LA%\Google\Chrome\Application\chrome.exe"
if not defined B goto :default

echo [open] %B%
start "" "%B%" "%F%"
goto :done

:default
echo [open] Edge/Chrome not found, using default handler.
start "" "%F%"
goto :done

rem ---------- foreground relay mode ----------
:foreground
if not exist "%SRV%" goto :nosrv
if not defined PY set "PY=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not exist "%PY%" goto :nopy
echo ==========================================================
echo   news-live  quote relay for the picker / dashboard
echo   http://127.0.0.1:8510   (/api/relay = quote relay)
echo   keep this window open  -  Ctrl+C to stop
echo ==========================================================
echo.
cd /d "%HERE%news-live"
"%PY%" server.py
echo.
echo service stopped.
pause
goto :done

:nosrv
echo [ERROR] server.py not found: %SRV%
pause
goto :done

:nopy
echo [ERROR] Python 3.10+ not found.
echo Need: pip install akshare pandas requests
pause
goto :done

:notfound
echo [ERROR] picker not found:
echo   %F%
pause
goto :done

:done
endlocal
exit /b 0
