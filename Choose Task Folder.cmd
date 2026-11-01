@echo off
title Local AI - Choose Task Folder
powershell.exe -NoLogo -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0Start-Hermes.ps1" -PickFolder
if errorlevel 1 pause
