@echo off
:: Syncs all addons from this repo to the network share.
:: Run this after committing and pushing to deliver updates to other users.

set "SRC=%~dp0"
set "DEST=X:\Temp\hector.silveri\blender_script"

if not exist "%DEST%" mkdir "%DEST%"

robocopy "%SRC%" "%DEST%" /MIR /NP /NFL /NDL /NJH /NJS /XD ".git" /XF "deploy.bat" "*.md" ".gitignore"

echo.
echo Deployed to %DEST%
echo Users will get the update on next Blender restart.
echo.
pause
