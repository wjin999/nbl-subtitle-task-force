@echo off
title NBL Subtitle Task Force - Build

set APP_VERSION=2.0.0

echo ============================================
echo   NBL Subtitle Task Force Build Script
echo ============================================
echo.

REM === 1. Check dependencies ===
echo [1/5] Checking dependencies...
python --version >nul 2>&1 || goto :err_python
node --version >nul 2>&1 || goto :err_node
cargo --version >nul 2>&1 || goto :err_rust
echo OK.

REM === 2. Install Python deps ===
echo [2/5] Installing Python dependencies...
python -m pip install -r requirements-build.txt
if %errorlevel% neq 0 goto :err_pip
echo OK.

REM === 3. Build Python backend ===
echo [3/5] Building api-server.exe (PyInstaller)...
if not exist "ui\src-tauri\bin" mkdir "ui\src-tauri\bin"
pyinstaller api-server.spec -y --distpath "ui\src-tauri\bin"
if %errorlevel% neq 0 goto :err_pyinstaller
if not exist "ui\src-tauri\bin\api-server.exe" goto :err_sidecar
for /f "tokens=2" %%T in ('rustc -vV ^| findstr /b "host:"') do set "TAURI_TARGET=%%T"
if "%TAURI_TARGET%"=="" goto :err_sidecar
copy /y "ui\src-tauri\bin\api-server.exe" "ui\src-tauri\bin\api-server-%TAURI_TARGET%.exe" >nul
if %errorlevel% neq 0 goto :err_sidecar
echo OK.
dir "ui\src-tauri\bin\api-server.exe"
dir "ui\src-tauri\bin\api-server-%TAURI_TARGET%.exe"

REM === 4. Install frontend deps ===
echo [4/5] Installing frontend dependencies...
cd ui
call npm install
if %errorlevel% neq 0 goto :err_npm
cd ..
echo OK.

REM === 5. Build Tauri app ===
echo [5/5] Building Tauri desktop app...
cd ui
call npm run tauri build
if %errorlevel% neq 0 goto :err_tauri
cd ..

echo.
echo ============================================
echo   Build complete!
echo   Output: ui\src-tauri\target\release\bundle\
echo ============================================
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
echo [ERROR] Rust not found. Install: https://rustup.rs
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

:err_tauri
cd ..
echo [ERROR] Tauri build failed
pause & exit /b 1
