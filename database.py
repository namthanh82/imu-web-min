import json, os, time
import threading
from datetime import datetime, timezone, timedelta

VN_TZ = timezone(timedelta(hours=7))

PATIENTS_FILE = "sample.json"
RECORD_FILE  = "records.json"
VAS_FILE = "vas.json"
EXPORT_DIR = "exports"

os.makedirs(EXPORT_DIR, exist_ok=True)

RECORD_LOCK = threading.Lock()
VAS_LOCK  = threading.Lock()

RECORD_STORE = []
VAS_STORE = []

def _ensure_patients_file():
    if not os.path.exists(PATIENTS_FILE):
        with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def load_patients_rows():
    _ensure_patients_file()
    with open(PATIENTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        data = {}
    rows = []
    for code, rec in data.items():
        rows.append({
            "code": code,
            "full_name": rec.get("name", ""),
            "dob": rec.get("DateOfBirth", ""),
            "national_id": rec.get("ID", ""),
            "sex": rec.get("Gender", ""),
        })
    rows = sorted(rows, key=lambda r: (r["full_name"] or "").lower())
    return rows, data

def save_patients_data(data):
    with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_records_from_file():
    global RECORD_STORE
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            RECORD_STORE = json.load(f)
            if not isinstance(RECORD_STORE, list):
                RECORD_STORE = []
    except FileNotFoundError:
        RECORD_STORE = []

def save_records_to_file():
    try:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(RECORD_STORE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[WARN] save_records_to_file error:", e)

def gen_patient_code(full_name: str) -> str:
    last = (full_name.split()[-1] if full_name else "BN")
    base = "".join(ch for ch in last if ch.isalnum())
    suffix = datetime.now().strftime("%m%d%H%M")
    return f"{base}{suffix}"