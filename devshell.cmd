@echo off
rem Open a shell in this project with the environment variable already set, for
rem running uv/pytest by hand without tripping over the OneDrive .venv problem.
rem   devshell            then: uv run pytest, uv run python -m gpsrtk, ...
set "UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\gps-rtk"
cd /d "%~dp0"
echo UV_PROJECT_ENVIRONMENT=%UV_PROJECT_ENVIRONMENT%
cmd /k
