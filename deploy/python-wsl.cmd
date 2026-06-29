@echo off
setlocal
for %%I in ("%~dp0..") do set "REPO_WIN=%%~fI"
for /f "usebackq delims=" %%P in (`wsl wslpath -a "%REPO_WIN%"`) do set "REPO_WSL=%%P"
wsl --cd "%REPO_WSL%" .venv/bin/python %*
