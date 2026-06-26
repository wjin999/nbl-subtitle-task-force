# -*- mode: python ; coding: utf-8 -*-

import sys, os, importlib
from pathlib import Path

# spaCy is a core feature — always bundle the English model.

SPACY_MODELS = (
    'en_core_web_sm',
)


def _find_spacy_model_dir(model_name):
    """Find a spaCy model installation path for bundling into exe."""
    try:
        model_module = importlib.import_module(model_name)
        return os.path.dirname(model_module.__file__)
    except ImportError:
        site_packages = Path(sys.executable).parent / 'Lib' / 'site-packages'
        model_dir = site_packages / model_name
        if model_dir.is_dir():
            return str(model_dir)
        return None


_datas = [('src/srt_translator', 'srt_translator')]
_hiddenimports = [
    'srt_translator',
    'srt_translator.config', 'srt_translator.models', 'srt_translator.parser',
    'srt_translator.merger', 'srt_translator.glossary', 'srt_translator.llm_client',
    'srt_translator.translator', 'srt_translator.text_utils', 'srt_translator.prompts',
    'srt_translator.agent_plan', 'srt_translator.streaming', 'srt_translator.streaming_pipeline',
    'spacy', 'spacy.lang.en', 'spacy.cli',
    'en_core_web_sm',
    'thinc',
]

for _model_name in SPACY_MODELS:
    _spacy_dir = _find_spacy_model_dir(_model_name)
    if _spacy_dir:
        _datas.append((_spacy_dir, _model_name))
        print(f"INFO: Bundling spaCy model {_model_name} from: {_spacy_dir}")
    else:
        print(f"WARNING: {_model_name} model not found. Install: python -m spacy download {_model_name}")

a = Analysis(
    ['api_server.py'],
    pathex=[],
    binaries=[],
    datas=_datas,
    hiddenimports=_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='api-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
