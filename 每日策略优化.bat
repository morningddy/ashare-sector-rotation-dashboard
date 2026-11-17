@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title A-Share Strategy Lab - Daily Optimize
set "HERE=%~dp0"
set "F=%HERE%ashare-sector-rotation-dashboard.html"
set "OUTDIR=%HERE%lab_reports"
if not exist "%F%" goto :notfound

rem ===========================================================
rem  Strategy Lab - daily optimize runner
rem    double-click          -> open dashboard, auto run optimize + scan (visible)
rem    每日策略优化.bat report -> headless run, write lab_reports\YYYY-MM-DD.md
rem  Scheduled task: run this bat with argument  report
rem ===========================================================

set "FP=%F:\=/%"
set "URL=file:///%FP%?page=lab&auto=1&topn=30"

if /I "%~1"=="report" goto :report

rem ---------- visible mode ----------
set "B="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "B=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined B if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "B=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined B if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "B=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined B goto :defaultbrowser
echo [open] %B%
echo [url ] %URL%
start "" "%B%" "%URL%"
goto :done

:defaultbrowser
echo [warn] Edge/Chrome not found, using default handler.
start "" "%URL%"
goto :done

rem ---------- headless report mode ----------
:report
set "EDGE="
if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not defined EDGE goto :noedge

set "TMPDOM=%TEMP%\lab_dom_%RANDOM%.html"
echo [run ] headless optimize + scan, please wait 1-3 minutes ...
"%EDGE%" --headless=new --disable-gpu --no-sandbox --allow-file-access-from-files --virtual-time-budget=600000 --dump-dom "%URL%&cfg=1" > "%TMPDOM%" 2>nul
if not exist "%TMPDOM%" goto :nodump
echo [dom ] %TMPDOM%

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$s=[IO.File]::ReadAllText('%TMPDOM%',[Text.Encoding]::UTF8);" ^
  "if($s -notmatch '@@LAB@@(.+?)</pre>'){ Write-Host '[fail] no export block - page likely did not finish (check lab_config.json / network)'; exit 1 };" ^
  "$raw=$matches[1] -replace '&quot;',[char]34 -replace '&amp;','&' -replace '&lt;','<' -replace '&gt;','>' -replace '&#39;',[char]39;" ^
  "$j=$raw | ConvertFrom-Json;" ^
  "if(-not $j.date){ Write-Host '[fail] export block unparsable'; exit 1 };" ^
  "if(-not (Test-Path '%OUTDIR%')){ New-Item -ItemType Directory '%OUTDIR%' | Out-Null };" ^
  "$pc=[string][char]37;" ^
  "$p=@(); foreach($k in 'p1','p2','p3'){ if($null -ne $j.params.$k){ $p += ($k + '=' + $j.params.$k) } };" ^
  "$md=@();" ^
  "$md += ('# 策略实验室日报 ' + $j.date);" ^
  "$md += '';" ^
  "$md += ('- 基准标的：' + $j.bench);" ^
  "$md += ('- 优化目标：' + $j.goal);" ^
  "$md += ('- 最优参数：' + ($p -join ', '));" ^
  "$md += ('- 样本内表现：收益 ' + $j.metrics.retPct + $pc + ' / 年化 ' + $j.metrics.ann + $pc + ' / 夏普 ' + $j.metrics.sharpe + ' / 最大回撤 ' + $j.metrics.mdd + $pc + ' / ' + $j.metrics.trades + ' 笔 / 胜率 ' + $j.metrics.winRate + $pc + ' / 买入持有 ' + $j.metrics.bh + $pc);" ^
  "$md += '';" ^
  "$md += ('## 今日信号股 (' + @($j.picks).Count + ')');" ^
  "$md += '';" ^
  "if(@($j.picks).Count -eq 0){ $md += '- 今日无命中信号' } else { foreach($x in $j.picks){ $md += ('- ' + $x.code + ' ' + $x.name + '  现价 ' + $x.price + '  涨跌 ' + $x.pct + $pc + '  信号日 ' + $x.sig) } };" ^
  "$md += '';" ^
  "$md += '> 历史数据统计推断，不构成投资建议。参数在同一段历史上择优，存在过拟合风险。';" ^
  "$f2=Join-Path '%OUTDIR%' ($j.date + '.md');" ^
  "[IO.File]::WriteAllLines($f2, $md, (New-Object Text.UTF8Encoding($false)));" ^
  "Write-Host ('[ok  ] report -> ' + $f2);" ^
  "Write-Host ('[pick] ' + @($j.picks).Count + ' signals, ret ' + $j.metrics.retPct + $pc + ', sharpe ' + $j.metrics.sharpe)"

del /q "%TMPDOM%" >nul 2>&1
goto :done

:noedge
echo [ERROR] Microsoft Edge not found. Install Edge or run the visible mode.
pause
exit /b 1

:nodump
echo [ERROR] headless run produced no output.
pause
exit /b 1

:notfound
echo [ERROR] dashboard not found: %F%
pause
exit /b 1

:done
echo.
echo done.
if /I "%~1"=="report" exit /b 0
pause
