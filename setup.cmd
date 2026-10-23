@echo off
rem dsh-pc-hands - one-command installer (ASCII only, no admin needed).
rem
rem   setup.cmd                 install scripts + Pillow
rem   setup.cmd -WithOCR        also install RapidOCR (better accuracy)
rem   setup.cmd -InstallPlugin  also register the plugin in the DSH profile
rem   setup.cmd -Check          only report what is ready
setlocal
set PS=powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
%PS% %*
if errorlevel 1 (
  echo.
  echo setup did not finish cleanly - see the FAIL lines above.
)
echo.
pause
