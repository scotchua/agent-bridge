@echo off
REM Windows entry point. The POSIX launcher next to this file is a /bin/sh
REM script, which Windows cannot execute: it has no shebang handling, so
REM running it yields "WinError 193: %1 is not a valid Win32 application".
setlocal
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
set "PYTHONDONTWRITEBYTECODE=1"
if not defined AGENT_BRIDGE_PYTHON set "AGENT_BRIDGE_PYTHON=python"
"%AGENT_BRIDGE_PYTHON%" -P -m agent_bridge.mcp_server %*
