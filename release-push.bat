@echo off
setlocal EnableExtensions DisableDelayedExpansion
title NBL Subtitle Task Force - Push and Release
cd /d "%~dp0"
if errorlevel 1 (
    echo [ERROR] Failed to enter script directory.
    pause
    exit /b 1
)

set APP_VERSION=2.0.0
set TAG_NAME=v%APP_VERSION%
set RELEASE_DIR=release\%APP_VERSION%

echo ============================================
echo   NBL Subtitle Task Force - Push and Release
echo ============================================
echo.

REM === Check for gh CLI ===
where gh >nul 2>&1
if errorlevel 1 (
    echo [ERROR] GitHub CLI ^(gh^) is not installed.
    echo.
    echo Install from: https://cli.github.com/
    echo Or run: winget install --id GitHub.cli
    echo Then run: gh auth login
    pause
    exit /b 1
)

gh auth status >nul 2>&1
if errorlevel 1 (
    echo [ERROR] GitHub CLI is not authenticated.
    echo Please run: gh auth login
    pause
    exit /b 1
)

call :verify_assets
if errorlevel 1 exit /b 1

REM === 1. Stage files ===
echo [1/4] Staging files...

REM Stage tracked changes plus known project files only.
REM This avoids accidentally committing local tool state or release artifacts.
git add -u
if errorlevel 1 goto :err_stage
git add ^
    .gitignore README.md pyproject.toml api_server.py api-server.spec ^
    build.bat release-build.bat release-push.bat glossary.example.txt ^
    src tests ^
    ui\index.html ui\package.json ui\package-lock.json ui\src ^
    ui\src-tauri\Cargo.toml ui\src-tauri\Cargo.lock ui\src-tauri\build.rs ^
    ui\src-tauri\capabilities ui\src-tauri\icons ui\src-tauri\src ui\src-tauri\tauri.conf.json
if errorlevel 1 goto :err_stage

echo ------ Files to commit ------
git status --short
echo -----------------------------
echo.

REM === 2. Commit ===
set /p COMMIT_MSG="[2/4] Commit message (press Enter for default): "
if "%COMMIT_MSG%"=="" set "COMMIT_MSG=Release v%APP_VERSION%"

git commit -m "%COMMIT_MSG%"
if errorlevel 1 goto :err_commit

REM === 3. Tag ===
echo.
echo [3/4] Creating tag %TAG_NAME%...

git rev-parse "%TAG_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [WARNING] Tag %TAG_NAME% already exists.
    echo To publish the current local changes as %TAG_NAME%, choose Y to recreate the tag.
    echo Choose N only if the existing tag already points to the commit you want to release.
    choice /c YN /m "Delete old tag and recreate"
    if errorlevel 2 goto :push
    git tag -d "%TAG_NAME%"
    if errorlevel 1 goto :err_tag_delete
    git push origin ":refs/tags/%TAG_NAME%"
    if errorlevel 1 goto :err_remote_tag_delete
    echo Old tag removed.
)

git tag -a "%TAG_NAME%" -m "NBL Subtitle Task Force v%APP_VERSION%"
if errorlevel 1 goto :err_tag_create
echo Tag %TAG_NAME% created.

REM === 4. Push ===
:push
echo.
echo [4/4] Pushing to GitHub...
git push origin main "%TAG_NAME%"
if errorlevel 1 (
    echo [ERROR] Push failed. Check network or permissions.
    pause
    exit /b 1
)
echo Push complete.

REM === Create GitHub Release ===
echo.
echo ------ Create GitHub Release ------

REM Check if installers exist
call :verify_assets
if errorlevel 1 exit /b 1

set "RELEASE_NOTES_FILE=%TEMP%\nbl-subtitle-task-force-release-notes-%APP_VERSION%.md"
(
    echo ## NBL Subtitle Task Force v%APP_VERSION%
    echo.
    echo ### Added
    echo - Reworked translation into a streaming NBL Agent workflow with window analysis, global AgentPlan, draft translation, review, audit, and report output.
    echo - Added NDJSON spool files for recoverable long-file processing.
    echo - Added automatic `.agent-report.json` translation reports.
    echo - Added an option to save spaCy merged source subtitles.
    echo.
    echo ### Removed
    echo - Removed Japanese and Korean spaCy model support; English smart sentence merging is now the only merge path.
    echo - Removed UI/API toggles for disabling merge or timeline review.
    echo.
    echo ### Fixed
    echo - Updated app naming and GitHub metadata for NBL Subtitle Task Force.
    echo.
    echo ### Build
    echo - Windows: .msi / .exe installer
    echo - Source: pip install -e .[all]
) > "%RELEASE_NOTES_FILE%"

echo Creating release with installer packages...
gh release create "%TAG_NAME%" ^
    --title "NBL Subtitle Task Force v%APP_VERSION%" ^
    --notes-file "%RELEASE_NOTES_FILE%" ^
    "%RELEASE_DIR%\*%APP_VERSION%*"

if not errorlevel 1 (
    echo.
    echo ============================================
    echo   Release published successfully!
    echo ============================================
) else (
    echo.
    echo [WARNING] Release creation failed.
    echo You can create it manually at:
    echo   https://github.com/wjin999/nbl-subtitle-task-force/releases/new
    echo   Select tag: %TAG_NAME%
)

echo.
pause
exit /b 0

:err_stage
echo [ERROR] Failed to stage files.
pause
exit /b 1

:err_commit
echo [ERROR] Commit failed. Check git status and try again.
pause
exit /b 1

:err_tag_delete
echo [ERROR] Failed to delete local tag %TAG_NAME%.
pause
exit /b 1

:err_remote_tag_delete
echo [ERROR] Failed to delete remote tag %TAG_NAME%.
pause
exit /b 1

:err_tag_create
echo [ERROR] Failed to create tag %TAG_NAME%.
pause
exit /b 1

:verify_assets
set HAS_MSI=0
set HAS_EXE=0
if exist "%RELEASE_DIR%\*%APP_VERSION%*.msi" set HAS_MSI=1
if exist "%RELEASE_DIR%\*%APP_VERSION%*.exe" set HAS_EXE=1
if %HAS_MSI% neq 1 (
    echo [ERROR] No MSI installer package for v%APP_VERSION% found in %RELEASE_DIR%.
    echo Run release-build.bat before publishing.
    pause
    exit /b 1
)
if %HAS_EXE% neq 1 (
    echo [ERROR] No EXE installer package for v%APP_VERSION% found in %RELEASE_DIR%.
    echo Run release-build.bat before publishing.
    pause
    exit /b 1
)
exit /b 0
