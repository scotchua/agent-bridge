@echo off
REM PreToolUse hook for Claude Code and the Codex CLI (delegation-first gate).
REM
REM Hook mode must never fail open. The gate itself always exits 0 and carries
REM its decision in the JSON on stdout, but that contract only starts once the
REM interpreter is running. On a stock Windows account with no Python, the
REM bare name "python" resolves to the Microsoft Store App Execution Alias
REM stub and this script exited 9009 with nothing on stdout; a host reads a
REM hook that produced no decision as a non-blocking error and runs the tool
REM anyway. So a launch failure is turned into a deny here.
setlocal
set "REPO=%~dp0.."
set "PYTHONPATH=%REPO%\src"
set "PYTHONDONTWRITEBYTECODE=1"
REM Both hosts emit raw UTF-8 and the gate decodes its payload as UTF-8
REM explicitly; this covers everything else in the process.
set "PYTHONUTF8=1"
if not defined AGENT_BRIDGE_PYTHON set "AGENT_BRIDGE_PYTHON=python"
set "SUB=%~1"
REM These are operator commands, so retain their ordinary stdout and exit
REM status instead of treating them as a hook decision.
if /i "%SUB%"=="install" goto run_subcommand
if /i "%SUB%"=="report" goto run_subcommand
if /i "%SUB%"=="audit" goto run_subcommand
if /i "%SUB%"=="-h" goto run_subcommand
if /i "%SUB%"=="--help" goto run_subcommand
set "OUTPUT=%TEMP%\agent-bridge-gate-hook-%RANDOM%%RANDOM%.json"
REM Buffer stdout until the child has exited cleanly.  A child that prints an
REM allow and then dies must not leave that allow beside our fail-closed deny.
"%AGENT_BRIDGE_PYTHON%" -P -m agent_bridge.orchestration.gate %* > "%OUTPUT%" 2>nul
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" goto validate_decision
goto phase5_check

:validate_decision
REM json.load requires one complete JSON document.  It accepts the gate's
REM empty allow or a single PreToolUse allow/deny and emits no raw output.
"%AGENT_BRIDGE_PYTHON%" -P -c "import json,sys; value=json.load(sys.stdin); decision=value.get('hookSpecificOutput') if isinstance(value,dict) else None; sys.exit(0 if isinstance(value,dict) and (not value or (isinstance(decision,dict) and decision.get('hookEventName') == 'PreToolUse' and decision.get('permissionDecision') in ('allow','deny'))) else 1)" < "%OUTPUT%" >nul 2>&1
if not errorlevel 1 (
  type "%OUTPUT%"
  del "%OUTPUT%" >nul 2>&1
  exit /b 0
)
del "%OUTPUT%" >nul 2>&1
set "FAILURE=hook returned no valid decision"
goto phase5_check

REM A PostToolUse invocation (Phase 5, inline output measurement) never
REM denies and the tool call it answers for has already finished, so the
REM PreToolUse-shaped deny hint below would be meaningless here; the same
REM argv-only mode selection the gate module itself uses.
REM
REM Checked positionally with %~1/%~2 and shift, rather than by piping %*
REM through findstr: a review found that re-expanding %* re-parses the
REM whole flattened argument line a second time, so an installed --config
REM path that happened to contain the literal text "--event PostToolUse"
REM (unlikely, but not impossible) would misclassify a real PreToolUse
REM call as PostToolUse at the one moment the interpreter has already
REM failed to start -- a silent {} instead of the fail-closed deny this
REM whole branch exists to guarantee. %~1/%~2 read this script's own
REM already-tokenized positional parameters instead, the same mechanism
REM %SUB% above already relies on.
:phase5_check
if "%~1"=="" goto phase5_not_post
if /i "%~1"=="--event" if /i "%~2"=="PostToolUse" goto phase5_is_post
shift
goto phase5_check
:phase5_is_post
del "%OUTPUT%" >nul 2>&1
echo {}
exit /b 0
:phase5_not_post
del "%OUTPUT%" >nul 2>&1
if not defined FAILURE set "FAILURE=child exit %RC%"
echo {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "delegation-first gate: the hook failed (%FAILURE%); nothing is implemented until the installation is repaired [gate_launcher_failed]"}}
exit /b 0

:run_subcommand
"%AGENT_BRIDGE_PYTHON%" -P -m agent_bridge.orchestration.gate %*
exit /b %ERRORLEVEL%
