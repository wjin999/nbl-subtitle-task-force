"""Release hardening regression tests.

These tests intentionally check release-critical source rules that are easy to
break in a desktop app without dedicated frontend or batch-script test runners.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_project_file(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_frontend_api_key_is_session_only() -> None:
    source = read_project_file("ui/src/hooks/useSettings.ts")

    assert 'load("apiKey"' not in source
    assert 'localStorage.setItem("apiKey"' not in source
    assert 'localStorage.removeItem("apiKey")' in source


def test_frontend_backend_config_fails_closed() -> None:
    source = read_project_file("ui/src/components/TranslationView.tsx")

    assert "fallbackBackendConfig" not in source
    assert "return fallbackBackendConfig" not in source
    assert "无法获取本地后台配置" in source


def test_release_push_validates_assets_before_git_mutation() -> None:
    source = read_project_file("release-push.bat")

    asset_check = source.index("call :verify_assets")
    first_git_mutation = min(
        source.index("git add -u"),
        source.index("git commit"),
        source.index("git tag"),
        source.index("git push origin main"),
    )

    assert asset_check < first_git_mutation


def test_release_build_fails_when_no_bundle_files_are_collected() -> None:
    source = read_project_file("release-build.bat")
    start = source.index("if %FOUND% equ 0")
    end = source.index("echo.", start)
    no_bundle_block = source[start:end]

    assert "exit /b 1" in no_bundle_block or "goto :err_no_bundles" in no_bundle_block


def test_build_scripts_use_pinned_build_requirements() -> None:
    requirements = read_project_file("requirements-build.txt")
    assert "pyinstaller==6.19.0" in requirements.lower()
    assert "en_core_web_sm-3.8.0-py3-none-any.whl" in requirements

    for script in ("build.bat", "release-build.bat"):
        source = read_project_file(script)
        lower = source.lower()
        assert "pip install -r requirements-build.txt" in lower
        assert "spacy download" not in lower
