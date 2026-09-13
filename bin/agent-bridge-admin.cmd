@echo off
setlocal
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
set "PYTHONDONTWRITEBYTECODE=1"
if not defined AGENT_BRIDGE_PYTHON set "AGENT_BRIDGE_PYTHON=python"
"%AGENT_BRIDGE_PYTHON%" -P -m agent_bridge.admin %*
