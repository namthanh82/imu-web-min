import os
from pathlib import Path

from chromadb.config import Settings
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
PERSIST_DIRECTORY = os.environ.get("PERSIST_DIRECTORY", "db")
PERSIST_PATH = Path(PERSIST_DIRECTORY)
if not PERSIST_PATH.is_absolute():
    PERSIST_PATH = BASE_DIR / PERSIST_PATH
PERSIST_PATH.mkdir(parents=True, exist_ok=True)

CHROMA_SETTINGS = Settings(
    chroma_db_impl="duckdb+parquet",
    persist_directory=str(PERSIST_PATH),
    anonymized_telemetry=False,
)
