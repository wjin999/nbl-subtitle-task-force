@echo off
setlocal enabledelayedexpansion
title NBL Subtitle Task Force - Release Build

set APP_VERSION=2.0.0
set RELEASE_DIR=release\%APP_VERSION%

echo ============================================
echo   NBL Subtitle Task Force v%APP_VERSION% Build
echo ============================================
echo.

REM === 1. Check dependencies ===
echo [1/6] Checking build tools...
python --version >nul 2>&1 || goto :err_python
node --version >nul 2>&1   || goto :err_node
cargo --version >nul 2>&1  || goto :err_rust
echo       OK.

REM === 2. Install Python build deps ===
echo [2/6] Installing Python build dependencies...
python -m pip install -r requirements-build.txt >nul 2>&1
if %errorlevel% neq 0 goto :err_pip
echo       OK.

REM === 3. Build Python backend (api-server.exe) ===
echo [3/6] Building api-server.exe (PyInstaller)...
if not exist "ui\src-tauri\bin" mkdir "ui\src-tauri\bin"
pyinstaller api-server.spec -y --distpath "ui\src-tauri\bin"
if %errorlevel% neq 0 goto :err_pyinstaller
if not exist "ui\src-tauri\bin\api-server.exe" (
    echo [ERROR] api-server.exe not found after build
    pause & exit /b 1
)
for /f "tokens=2" %%T in ('rustc -vV ^| findstr /b "host:"') do set "TAURI_TARGET=%%T"
if "%TAURI_TARGET%"=="" goto :err_sidecar
copy /y "ui\src-tauri\bin\api-server.exe" "ui\src-tauri\bin\api-server-%TAURI_TARGET%.exe" >nul
if %errorlevel% neq 0 goto :err_sidecar
for %%A in ("ui\src-tauri\bin\api-server.exe") do (
    set /a SIZE=%%~zA / 1048576
    echo       api-server.exe built ^(!SIZE! MB^)
)
for %%A in ("ui\src-tauri\bin\api-server-%TAURI_TARGET%.exe") do (
    set /a SIZE=%%~zA / 1048576
    echo       Tauri sidecar api-server-%TAURI_TARGET%.exe ready ^(!SIZE! MB^)
)

REM === 4. Install frontend deps ===
echo [4/6] Installing frontend dependencies...
cd ui
call npm install >nul 2>&1
if %errorlevel% neq 0 goto :err_npm
cd ..
echo       OK.

REM === 5. Build Tauri desktop app ===
echo [5/6] Building Tauri desktop app (this may take 3-10 min)...
set BUNDLE_DIR=ui\src-tauri\target\release\bundle
if exist "%BUNDLE_DIR%\msi\*.msi" del /q "%BUNDLE_DIR%\msi\*.msi" >nul 2>&1
if exist "%BUNDLE_DIR%\nsis\*.exe" del /q "%BUNDLE_DIR%\nsis\*.exe" >nul 2>&1
if exist "%BUNDLE_DIR%\dmg\*.dmg" del /q "%BUNDLE_DIR%\dmg\*.dmg" >nul 2>&1
cd ui
call npm run tauri build
set BUILD_RESULT=%errorlevel%
cd ..

if %BUILD_RESULT% neq 0 goto :err_tauri_build

REM === 6. Collect release artifacts ===
echo [6/6] Collecting release artifacts...
if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%" >nul 2>&1
mkdir "%RELEASE_DIR%" 2>nul

set FOUND=0

if exist "%BUNDLE_DIR%\msi\*%APP_VERSION%*.msi" (
    for %%f in ("%BUNDLE_DIR%\msi\*%APP_VERSION%*.msi") do (
        copy "%%f" "%RELEASE_DIR%\" >nul 2>&1
        echo       + %%~nxf
        set FOUND=1
    )
)
if exist "%BUNDLE_DIR%\nsis\*%APP_VERSION%*.exe" (
    for %%f in ("%BUNDLE_DIR%\nsis\*%APP_VERSION%*.exe") do (
        copy "%%f" "%RELEASE_DIR%\" >nul 2>&1
        echo       + %%~nxf
        set FOUND=1
    )
)
if exist "%BUNDLE_DIR%\dmg\*%APP_VERSION%*.dmg" (
    for %%f in ("%BUNDLE_DIR%\dmg\*%APP_VERSION%*.dmg") do (
        copy "%%f" "%RELEASE_DIR%\" >nul 2>&1
        echo       + %%~nxf
        set FOUND=1
    )
)

if %FOUND% equ 0 (
    echo       ERROR: No bundle files found at %BUNDLE_DIR%
    echo       Check that Tauri build completed successfully.
    goto :err_no_bundles
)

echo.
echo ============================================
echo   Build complete!
echo   Release files: %RELEASE_DIR%\
echo ============================================
dir /b "%RELEASE_DIR%" 2>nul
echo.
pause
exit /b 0

REM === Error handlers ===
:err_python
echo [ERROR] Python not found. Install Python 3.10+
pause & exit /b 1

:err_node
echo [ERROR] Node.js not found. Install Node.js
pause & exit /b 1

:err_rust
echo [ERROR] Rust/Cargo not found. Install Rust from https://rustup.rs
pause & exit /b 1

:err_pip
echo [ERROR] pip install failed
pause & exit /b 1

:err_pyinstaller
echo [ERROR] PyInstaller build failed
pause & exit /b 1

:err_sidecar
echo [ERROR] Failed to prepare Tauri sidecar executable
pause & exit /b 1

:err_npm
cd ..
echo [ERROR] npm install failed
pause & exit /b 1

:err_tauri_build
echo.
echo [ERROR] Tauri build failed - code: %BUILD_RESULT%
echo.
echo Common causes:
echo   1. Missing Windows SDK - install Visual Studio Build Tools
echo   2. api-server.exe not placed in ui/src-tauri/bin/
echo   3. Network issue when downloading cargo crates
pause & exit /b 1

:err_no_bundles
echo [ERROR] Release artifacts were not collected.
pause & exit /b 1
