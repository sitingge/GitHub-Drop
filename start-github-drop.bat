@echo off
setlocal
cd /d "%~dp0"
title GitHub Drop

echo.
echo   GitHub Drop - drag files/folders, upload to your GitHub repo
echo   The browser page opens automatically.
echo   Keep this window open while uploading; close it to stop the server.
echo.

where py >nul 2>nul
if %errorlevel%==0 goto use_py
where python >nul 2>nul
if %errorlevel%==0 goto use_python
goto no_python

:use_py
py -3 "%~dp0github_drop.py" %*
goto done

:use_python
python "%~dp0github_drop.py" %*
goto done

:no_python
echo [x] Python not found.
echo     Install Python 3.8+ and tick "Add python.exe to PATH", then run again.
echo     https://www.python.org/downloads/

:done
echo.
pause
