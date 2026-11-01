@echo off
title Local AI - Hermes
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Hermes.ps1"
if errorlevel 1 pause
