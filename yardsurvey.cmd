@echo off
rem Launch the app with the virtual environment kept OUTSIDE OneDrive.
rem
rem This wrapper exists because uv only accepts UV_PROJECT_ENVIRONMENT as an
rem environment variable - there is no uv.toml key for it, and uv's .env support
rem loads variables for the command being run, not for uv's own configuration.
rem Without it, `uv run` creates .venv inside this OneDrive-synced folder,
rem OneDrive dehydrates the files into cloud placeholders, and uv then fails
rem with "Access is denied" when it tries to update them.
rem
rem Usage:  yardsurvey                     start empty
rem         yardsurvey "archive\Project 1.zip"
setlocal
set "UV_PROJECT_ENVIRONMENT=%USERPROFILE%\.venvs\gps-rtk"
cd /d "%~dp0"

if not exist "%UV_PROJECT_ENVIRONMENT%\pyvenv.cfg" (
    echo Creating the environment at %UV_PROJECT_ENVIRONMENT% ...
    uv sync || exit /b 1
)

uv run yardsurvey %*
endlocal
