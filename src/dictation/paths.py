from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
TIP_WORD_ROOT = PACKAGE_ROOT / "vendor" / "tip_word"
DATA_ROOT = PROJECT_ROOT / "dictation_data"
CATALOG_DIR = DATA_ROOT / "catalog"
SOURCES_DIR = DATA_ROOT / "sources"
HISTORY_DIR = DATA_ROOT / "history"
PARENT_DIR = DATA_ROOT / "parent_uploads"
TEMP_DIR = DATA_ROOT / "tmp"
TTS_CACHE_DIR = PROJECT_ROOT / "cache" / "dictation_tts"


def ensure_runtime_dirs() -> None:
    for path in (HISTORY_DIR, PARENT_DIR, TEMP_DIR, TTS_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
