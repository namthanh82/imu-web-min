
# webgiaodien.py
import os, json, time, math, io, csv, threading
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from collections import defaultdict, deque

from flask import Flask, request, jsonify, render_template_string, redirect, url_for, flash, send_file
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

from flask_socketio import SocketIO, emit

# =========================
#   GLOBALS / CONSTANTS
# =========================
VN_TZ = timezone(timedelta(hours=7))

DATA_LOCK = threading.Lock()
MAX_LOCK  = threading.Lock()
EMG_LOCK  = threading.Lock()
VAS_LOCK  = threading.Lock()
RECORD_LOCK = threading.Lock()

data_buffer = []          # samples đang đo
LAST_SESSION = []         # samples phiên gần nhất

MAX_ANGLES = {"hip": 0.0, "knee": 0.0, "ankle": 0.0}

EMG_BUF = deque(maxlen=200)     # RMS window ~200 mẫu
EMG_ENV = 0.0
EMG_ALPHA = 0.1

LAST_EMG = {"emg": None}        # giữ EMG gần nhất (object {v,t_ms,sender_id})

VAS_STORE = []
VAS_FILE  = "vas.json"

RECORD_STORE = []
RECORD_FILE  = "records.json"

PATIENTS_FILE = "sample.json"
EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)

# ========== SERIAL ==========
SERIAL_ENABLED = True

pyserial = None
list_ports = None
try:
    import serial as pyserial
    from serial.tools import list_ports
except Exception:
    SERIAL_ENABLED = False

ser = None
serial_thread = None
stop_serial_thread = False


# =========================
#   SIMPLE HTML PLACEHOLDER
#   (Bạn thay DASH_HTML bằng bản của bạn)
# =========================
LOGIN_HTML = """<!doctype html><html><body>
<h3>Login</h3>
<form method="post">
<input name="username" placeholder="user"><br>
<input name="password" type="password" placeholder="pass"><br>
<button>Login</button>
</form>
{% if error_message %}<p style="color:red">{{error_message}}</p>{% endif %}
</body></html>"""

# ⚠️ Bạn đang có DASH_HTML rất dài — giữ nguyên bản bạn đang dùng.
# Ở đây chỉ placeholder để file chạy được.
DASH_HTML = """<!doctype html><html><body>
<h3>Dashboard placeholder</h3>
<p>Xin chào, {{username}}</p>
<p>Hãy thay DASH_HTML trong file này bằng bản UI của bạn.</p>
</body></html>"""

CHARTS_HTML = """<!doctype html><html><body>
<h3>Charts placeholder</h3>
<p>exercise={{exercise_name}} patient={{patient_code}}</p>
</body></html>"""

CALIBRATION_HTML = """<!doctype html><html><body><h3>Calibration</h3></body></html>"""
PATIENT_NEW_HTML = """<!doctype html><html><body><h3>Patients</h3></body></html>"""
PATIENTS_MANAGE_HTML = """<!doctype html><html><body><h3>Manage Patients</h3></body></html>"""
RECORD_HTML = """<!doctype html><html><body><h3>Records</h3></body></html>"""
SETTINGS_HTML = """<!doctype html><html><body><h3>Settings</h3></body></html>"""
EMG_CHART_HTML = """<!doctype html><html><body><h3>EMG Charts</h3></body></html>"""


# =========================
#   HELPERS
# =========================
def _ensure_patients_file():
    if not os.path.exists(PATIENTS_FILE):
        with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def gen_patient_code(full_name: str) -> str:
    last = (full_name.split()[-1] if full_name else "BN")
    base = "".join(ch for ch in last if ch.isalnum())
    suffix = datetime.now().strftime("%m%d%H%M")
    return f"{base}{suffix}"

def norm_deg(x: float) -> float:
    while x > 180:
        x -= 360
    while x < -180:
        x += 360
    return x

def clamp(val, lo, hi):
    return max(lo, min(hi, val))

def emg_rms(buf):
    if not buf:
        return 0.0
    return math.sqrt(sum(x*x for x in buf) / len(buf))

def reset_max_angles():
    with MAX_LOCK:
        MAX_ANGLES["hip"] = 0.0
        MAX_ANGLES["knee"] = 0.0
        MAX_ANGLES["ankle"] = 0.0

def auto_detect_port():
    if not list_ports:
        return None
    ports = list(list_ports.comports())
    for p in ports:
        if any(x in (p.description or "").upper() for x in ["USB", "ACM", "CP210", "CH340", "UART", "SERIAL"]):
            return p.device
    return ports[0].device if ports else None


# =========================
#   HIP STATE (pitch2)
# =========================
HIP_STATE    = {"mode": "front", "prev_pitch2": 0.0}
PITCH_MID    = 90.0
PITCH_HYS    = 10.0
HIP_CROSS_TH = 40.0
DEADZONE     = 2.0


# =========================
#   SMOOTH FILTER
# =========================
_SMOOTH_STATE = {"hip": 0.0, "knee": 0.0, "ankle": 0.0}
_SMOOTH_ALPHA = {"hip": 0.25, "knee": 0.25, "ankle": 0.25}

def _smooth(key: str, x: float) -> float:
    a = _SMOOTH_ALPHA.get(key, 0.25)
    prev = _SMOOTH_STATE.get(key, x)
    y = a * x + (1 - a) * prev
    _SMOOTH_STATE[key] = y
    return y


# =========================
#   SERIAL PARSER
# =========================
def parse_serial_line(line: str):
    # IMU,sender_id,timestamp_ms,yaw,roll,pitch
    # EMG,sender_id,timestamp_us,emg_clean
    parts = [p.strip() for p in line.strip().split(",") if p.strip() != ""]
    if not parts:
        return None

    tag = parts[0].upper()

    try:
        if tag == "IMU" and len(parts) >= 6:
            sender_id = int(parts[1])
            ts = int(float(parts[2]))  # đôi khi gửi "123.0"
            yaw = float(parts[3])
            roll = float(parts[4])
            pitch = float(parts[5])
            return ("imu", sender_id, ts, yaw, roll, pitch)

        if tag == "EMG" and len(parts) >= 4:
            sender_id = int(parts[1])
            ts_us = int(float(parts[2]))
            emg_clean = float(parts[3])
            return ("emg", sender_id, ts_us, emg_clean)
    except Exception:
        return None

    return None


# =========================
#   SERIAL START/STOP (FIX PermissionError)
# =========================
def stop_serial_reader():
    global ser, serial_thread, stop_serial_thread
    stop_serial_thread = True

    # đóng port trước để tránh ClearCommError / Access denied
    try:
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
    finally:
        ser = None

    # join thread
    try:
        if serial_thread and serial_thread.is_alive():
            serial_thread.join(timeout=1.0)
    except Exception:
        pass

    serial_thread = None
    return True


def start_serial_reader(port=None, baud=115200):
    """Đọc serial và gọi append_samples(); timestamp đồng nhất theo host time (ms)."""
    global ser, serial_thread, stop_serial_thread

    if not SERIAL_ENABLED or pyserial is None:
        print("[SERIAL] pyserial not available")
        return False

    if not port:
        port = os.environ.get("SERIAL_PORT") or auto_detect_port()

    if not port:
        print("Không tìm thấy cổng serial nào.")
        return False

    # đảm bảo không mở chồng
    stop_serial_reader()

    try:
        ser = pyserial.Serial(port, baud, timeout=0.5)
        # clear buffers
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        print(f"✅ Đã mở {port} @ {baud}")
    except Exception as e:
        print("❌ Không mở được cổng serial:", e)
        return False

    stop_serial_thread = False
    last_angles = defaultdict(lambda: {"yaw": 0.0, "roll": 0.0, "pitch": 0.0, "ts": 0.0})

    def reader_loop():
        print(f"📥 Đang đọc dữ liệu từ {port} @ {baud} ...")
        global stop_serial_thread

        while not stop_serial_thread:
            try:
                raw = ser.readline()
                if not raw:
                    continue

                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                parsed = parse_serial_line(line)
                if not parsed:
                    continue

                ptype = parsed[0]
                now_ms = time.time() * 1000.0  # ✅ timebase CHUNG

                if ptype == "imu":
                    _, sid, ts, yaw, roll, pitch = parsed

                    last_angles[sid] = {
                        "yaw": yaw, "roll": roll, "pitch": pitch, "ts": ts
                    }

                    p1 = last_angles.get(1, {}).get("roll", 0.0)
                    p2 = last_angles.get(2, {}).get("roll", 0.0)
                    p3 = last_angles.get(3, {}).get("roll", 0.0)
                    p4 = -last_angles.get(4, {}).get("roll", 0.0)
                    pitch2 = last_angles.get(2, {}).get("pitch", 0.0)

                    raw_hip = norm_deg(p2 - p1)
                    raw_knee = norm_deg(p3 - p2)
                    raw_ankle = norm_deg(p4 - p3)

                    append_samples([{
                        "t_ms": now_ms,
                        "hip": raw_hip,
                        "knee": raw_knee,
                        "ankle": raw_ankle,
                        "pitch2": pitch2
                    }])

                elif ptype == "emg":
                    _, sender_id, ts_us, emg_clean = parsed
                    emg_rect = abs(float(emg_clean))

                    emg_entry = {
                        "v": emg_rect,
                        "t_ms": now_ms,
                        "sender_id": int(sender_id)
                    }

                    with EMG_LOCK:
                        EMG_BUF.append(emg_rect)
                        LAST_EMG["emg"] = emg_entry

            except Exception as e:
                # nếu port bị rút ra hoặc bị close, thoát vòng lặp
                msg = str(e)
                print("Serial read error:", e)
                if "ClearCommError" in msg or "Access is denied" in msg:
                    break

        print("🛑 Dừng đọc serial")

    serial_thread = threading.Thread(target=reader_loop, daemon=True)
    serial_thread.start()
    return True


# =========================
#   APPEND SAMPLES + EMIT
# =========================
def append_samples(samples):
    """Xử lý hip/knee/ankle + sync EMG, rồi emit socket 'imu_data'."""
    global EMG_ENV, HIP_STATE

    SYNC_WIN_MS = 80       # ✅ rộng hơn chút để chắc ăn khi PC lag
    EMG_SENSOR_ID = 5

    for s in samples:
        t_ms = float(s.get("t_ms", time.time() * 1000.0))

        raw_hip = float(s.get("hip", 0.0))
        knee    = float(s.get("knee", 0.0))
        ankle   = float(s.get("ankle", 0.0))
        pitch2  = float(s.get("pitch2", 0.0))

        # ---- hip sign theo pitch2
        mode = HIP_STATE.get("mode", "front")
        if abs(raw_hip) < HIP_CROSS_TH:
            if pitch2 <= (PITCH_MID - PITCH_HYS):
                mode = "front"
            elif pitch2 >= (PITCH_MID + PITCH_HYS):
                mode = "back"
        HIP_STATE["mode"] = mode
        sign_front = 1 if mode == "front" else -1

        mag_hip = abs(raw_hip)
        hip = 0.0 if mag_hip < DEADZONE else sign_front * mag_hip

        hip   = clamp(hip,  -30.1, 122.1)
        knee  = clamp(abs(knee),   0, 134)
        ankle = clamp(abs(ankle), 36, 113)

        hip   = _smooth("hip", hip)
        knee  = _smooth("knee", knee)
        ankle = _smooth("ankle", ankle)

        # ---- max angles
        with MAX_LOCK:
            if hip   > MAX_ANGLES["hip"]:   MAX_ANGLES["hip"]   = hip
            if knee  > MAX_ANGLES["knee"]:  MAX_ANGLES["knee"]  = knee
            if ankle > MAX_ANGLES["ankle"]: MAX_ANGLES["ankle"] = ankle

            max_payload = {
                "maxHip":   MAX_ANGLES["hip"],
                "maxKnee":  MAX_ANGLES["knee"],
                "maxAnkle": MAX_ANGLES["ankle"],
            }

        # ---- sync EMG: lấy LAST_EMG gần nhất theo host-time
        emg_v = None
        emg_id = None

        with EMG_LOCK:
            emg = LAST_EMG.get("emg")

        if emg:
            try:
                emg_id = int(emg.get("sender_id", -1))
                if emg_id == EMG_SENSOR_ID:
                    if abs(float(emg.get("t_ms", 0)) - t_ms) <= SYNC_WIN_MS:
                        emg_v = float(emg.get("v", 0.0))
            except Exception:
                emg_v = None
                emg_id = None

        # ---- RMS & envelope (nếu có emg_v)
        if emg_v is not None:
            rms = emg_rms(EMG_BUF)
            EMG_ENV = EMG_ALPHA * rms + (1 - EMG_ALPHA) * EMG_ENV
            max_payload["emg"] = emg_v                 # ✅ emg là SỐ (frontend vẽ được)
            max_payload["sender_id"] = emg_id          # ✅ cho JS lọc sensor 5
            max_payload["emg_id"] = emg_id
            max_payload["emg_rms"] = rms
            max_payload["emg_env"] = EMG_ENV

        # ---- lưu buffer
        with DATA_LOCK:
            data_buffer.append({
                "t_ms": t_ms,
                "hip": hip,
                "knee": knee,
                "ankle": ankle,
                "emg": max_payload.get("emg"),
                "emg_id": max_payload.get("emg_id"),
                "emg_rms": max_payload.get("emg_rms"),
                "emg_env": max_payload.get("emg_env"),
            })

        # ---- emit ra UI
        socketio.emit("imu_data", {
            "t": t_ms,
            "hip": hip,
            "knee": knee,
            "ankle": ankle,
            **max_payload
        })


# =========================
#   RECORDS LOAD/SAVE
# =========================
def load_records_from_file():
    global RECORD_STORE
    try:
        with open(RECORD_FILE, "r", encoding="utf-8") as f:
            RECORD_STORE = json.load(f)
            if not isinstance(RECORD_STORE, list):
                RECORD_STORE = []
    except FileNotFoundError:
        RECORD_STORE = []
    except Exception as e:
        print("[WARN] load_records_from_file error:", e)
        RECORD_STORE = []

def save_records_to_file():
    try:
        with open(RECORD_FILE, "w", encoding="utf-8") as f:
            json.dump(RECORD_STORE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[WARN] save_records_to_file error:", e)


# =========================
#   APP / SOCKET
# =========================
app = Flask(__name__)
app.secret_key = "CHANGE_ME"

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_interval=10,
    ping_timeout=30,
    async_mode="threading",
)

@socketio.on("connect")
def _on_connect():
    print("[SOCKET] client connected")
    emit("imu_data", {"t": time.time() * 1000, "hip": 0, "knee": 0, "ankle": 0})


# =========================
#   LOGIN
# =========================
login_manager = LoginManager(app)
login_manager.login_view = "login"
USERS = {"komlab": generate_password_hash("123456")}

class User(UserMixin):
    def __init__(self, u): self.id = u

@login_manager.user_loader
def load_user(u):
    return User(u) if u in USERS else None


# =========================
#   VIDEOS (FIX space -> underscore)
# =========================
EXERCISE_VIDEOS = {
    "ankle flexion": "/static/videos/ankle_flexion.mp4",
    "knee flexion":  "/static/knee flexion.mp4",
    "hip flexion":   "/static/videos/hip_flexion.mp4",
}


# =========================
#   ROUTES
# =========================
@app.route("/login", methods=["GET", "POST"])
def login():
    error_message = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u in USERS and check_password_hash(USERS[u], p):
            login_user(User(u))
            return redirect(url_for("dashboard"))
        error_message = "Sai tài khoản hoặc mật khẩu"
    return render_template_string(LOGIN_HTML, error_message=error_message)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASH_HTML, username=current_user.id, videos=EXERCISE_VIDEOS)

@app.route("/settings")
@login_required
def settings_page():
    return render_template_string(SETTINGS_HTML, username=current_user.id)

@app.route("/calibration")
@login_required
def calibration():
    open_guide = request.args.get("guide", "0") in ("1", "true", "yes")
    return render_template_string(CALIBRATION_HTML, username=current_user.id, open_guide=open_guide)

@app.route("/ports")
@login_required
def ports():
    if not list_ports:
        return jsonify(ports=[])
    items = [{"device": p.device, "desc": p.description} for p in list_ports.comports()]
    return jsonify(ports=items)

@app.post("/session/start")
@login_required
def session_start():
    global data_buffer
    data_buffer = []
    reset_max_angles()

    if SERIAL_ENABLED:
        port = os.environ.get("SERIAL_PORT") or "COM7"
        baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        ok = start_serial_reader(port=port, baud=baud)
        if not ok:
            return jsonify(ok=False, msg=f"Không mở được cổng serial (port={port})"), 500
        return jsonify(ok=True, mode="serial", port=port, baud=baud)

    return jsonify(ok=True, mode="noserial")

@app.post("/session/stop")
@login_required
def session_stop():
    global LAST_SESSION

    if SERIAL_ENABLED:
        stop_serial_reader()

    with DATA_LOCK:
        LAST_SESSION = list(data_buffer)
        data_buffer.clear()

    print(f"[SESSION STOP] saved {len(LAST_SESSION)} samples")
    return jsonify(ok=True, msg="Đã kết thúc phiên đo")

@app.post("/session/reset_max")
@login_required
def session_reset_max():
    reset_max_angles()
    socketio.emit("imu_data", {
        "t": time.time() * 1000,
        "maxHip": 0.0, "maxKnee": 0.0, "maxAnkle": 0.0
    })
    return jsonify(ok=True)

@app.post("/session/mock")
@login_required
def session_mock():
    for i in range(80):
        append_samples([{
            "t_ms": time.time() * 1000,
            "hip": 10 + i * 0.2,
            "knee": 20 + i * 0.15,
            "ankle": 60 + i * 0.1,
            "pitch2": 85
        }])
        time.sleep(0.03)
    return jsonify(ok=True, mode="mock")

@app.get("/session/export_csv")
@login_required
def session_export_csv():
    patient_code = request.args.get("patient_code", "").strip()

    with DATA_LOCK:
        rows = list(LAST_SESSION) if LAST_SESSION else list(data_buffer)

    sio = io.StringIO()
    w = csv.writer(sio)
    # ✅ header đúng
    w.writerow(["t_ms", "hip", "knee", "ankle", "emg", "emg_rms", "emg_env", "emg_id"])

    for r in rows:
        w.writerow([
            int(r.get("t_ms", 0)),
            f'{float(r.get("hip", 0)):.4f}',
            f'{float(r.get("knee", 0)):.4f}',
            f'{float(r.get("ankle", 0)):.4f}',
            "" if r.get("emg") is None else f'{float(r.get("emg", 0)):.5f}',
            "" if r.get("emg_rms") is None else f'{float(r.get("emg_rms", 0)):.5f}',
            "" if r.get("emg_env") is None else f'{float(r.get("emg_env", 0)):.5f}',
            "" if r.get("emg_id") is None else str(r.get("emg_id")),
        ])

    csv_text = sio.getvalue()
    data = io.BytesIO(csv_text.encode("utf-8-sig"))
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    safe_code = "".join(ch for ch in patient_code if ch.isalnum() or ch in ("-", "_"))
    filename = f"{safe_code}_{ts}_{len(rows)}rows.csv" if safe_code else f"imu_{ts}_{len(rows)}rows.csv"

    # lưu ra disk
    try:
        disk_path = os.path.join(EXPORT_DIR, filename)
        with open(disk_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(csv_text)
    except Exception as e:
        print("[WARN] cannot save CSV to disk:", e)

    data.seek(0)
    return send_file(
        data,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )

# ========= Patients API (giữ như bạn đang dùng) =========
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

@app.get("/api/patients")
@login_required
def api_patients_all():
    rows, raw = load_patients_rows()
    return jsonify(rows=rows, raw=raw)

@app.post("/api/patients")
@login_required
def api_patients_save():
    data = request.get_json(force=True) or {}
    code = (data.get("patient_code") or "").strip()
    full_name = (data.get("name") or "").strip()
    if not full_name:
        return jsonify(ok=False, msg="Thiếu họ tên"), 400

    _, raw = load_patients_rows()
    if not code:
        code = gen_patient_code(full_name)

    sex = (data.get("gender") or "").strip()
    if sex.lower().startswith("m"):
        sex = "Male"
    elif sex.lower().startswith("f"):
        sex = "FeMale"

    raw[code] = {
        "DateOfBirth": data.get("dob") or "",
        "Exercise": raw.get(code, {}).get("Exercise", {}),
        "Gender": sex,
        "Height": data.get("height") or "",
        "ID": data.get("national_id") or "",
        "PatientCode": code,
        "Weight": data.get("weight") or "",
        "name": full_name
    }
    with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    return jsonify(ok=True, patient_code=code)

# ========= VAS =========
@app.route("/save_vas", methods=["POST"])
def save_vas():
    data = request.get_json(silent=True) or {}
    try:
        vas = float(data.get("vas", None))
    except (TypeError, ValueError):
        return jsonify(ok=False, msg="Giá trị VAS không hợp lệ"), 400

    exercise_region = (data.get("exercise_region") or "").strip()
    phase           = (data.get("phase") or "").strip()
    exercise_name   = (data.get("exercise_name") or "").strip()
    patient_code    = (data.get("patient_code") or "").strip()

    if phase not in ("before", "after"):
        return jsonify(ok=False, msg="Sai phase (before/after)."), 400
    if not exercise_region:
        return jsonify(ok=False, msg="Thiếu exercise_region."), 400

    rec = {
        "patient_code": patient_code or None,
        "exercise_name": exercise_name or None,
        "exercise_region": exercise_region,
        "phase": phase,
        "vas": vas,
        "ts": time.time(),
    }
    with VAS_LOCK:
        VAS_STORE.append(rec)

    print("== VAS saved ==", rec)
    return jsonify(ok=True)

# ========= Records =========
load_records_from_file()

@app.post("/api/save_record")
@login_required
def api_save_record():
    global RECORD_STORE
    data = request.get_json(force=True) or {}

    patient_code    = (data.get("patient_code") or "").strip()
    measure_date    = data.get("measure_date") or ""
    patient_info    = data.get("patient_info") or {}
    exercise_scores = data.get("exercise_scores") or {}

    vas_summary = {}
    with VAS_LOCK:
        for row in VAS_STORE:
            pc_row = (row.get("patient_code") or "").strip()
            ex     = (row.get("exercise_name") or "").strip()
            ph     = (row.get("phase") or "").strip()
            val    = row.get("vas")

            if patient_code and pc_row and pc_row != patient_code:
                continue
            if not ex or ph not in ("before", "after"):
                continue
            vas_summary.setdefault(ex, {"before": None, "after": None})
            vas_summary[ex][ph] = val

    now = datetime.now(VN_TZ)
    record = {
        "created_at_ts": now.timestamp(),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "patient_code": patient_code,
        "measure_date": measure_date,
        "patient_info": patient_info,
        "exercise_scores": exercise_scores,
        "vas_summary": vas_summary,
    }

    with RECORD_LOCK:
        RECORD_STORE.append(record)
        save_records_to_file()

    return jsonify(ok=True, msg="saved", record=record)

@app.route("/records")
@login_required
def records():
    with RECORD_LOCK:
        rows = list(RECORD_STORE)
    rows.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)
    for r in rows:
        if "vas_summary" not in r or r["vas_summary"] is None:
            r["vas_summary"] = {}
    return render_template_string(RECORD_HTML, username=current_user.id, records=rows)


# ========= Charts =========
def _exercise_region_from_name(name: str):
    n = (name or "").lower()
    if "hip" in n: return "hip"
    if "knee" in n: return "knee"
    if "ankle" in n: return "ankle"
    return None

@app.route("/charts")
@login_required
def charts():
    global LAST_SESSION

    patient_code  = request.args.get("patient_code", "").strip()
    exercise_name = request.args.get("exercise", "").strip()

    vas_before = None
    vas_after = None
    region = _exercise_region_from_name(exercise_name)

    if region is not None:
        with VAS_LOCK:
            for rec in reversed(VAS_STORE):
                if rec.get("exercise_region") != region:
                    continue
                if patient_code and rec.get("patient_code") != patient_code:
                    continue
                ph = rec.get("phase")
                if ph == "before" and vas_before is None:
                    vas_before = rec.get("vas")
                elif ph == "after" and vas_after is None:
                    vas_after = rec.get("vas")
                if vas_before is not None and vas_after is not None:
                    break

    if not LAST_SESSION:
        return render_template_string(
            CHARTS_HTML,
            username=current_user.id,
            t_ms=[], hip=[], knee=[], ankle=[],
            emg=[], emg_rms=[], emg_env=[],
            patient_code=patient_code,
            exercise_name=exercise_name,
            vas_before=vas_before, vas_after=vas_after,
        )

    rows = list(LAST_SESSION)
    rows.sort(key=lambda x: x["t_ms"])
    raw_t = [r["t_ms"] for r in rows]
    t0 = raw_t[0] if raw_t else 0
    t_ms = [round((t - t0) / 1000.0, 3) for t in raw_t]

    hipArr    = [r.get("hip", 0.0) for r in rows]
    kneeArr   = [r.get("knee", 0.0) for r in rows]
    ankleArr  = [r.get("ankle", 0.0) for r in rows]
    emgArr    = [r.get("emg", 0.0) or 0.0 for r in rows]
    emgRmsArr = [r.get("emg_rms", 0.0) or 0.0 for r in rows]
    emgEnvArr = [r.get("emg_env", 0.0) or 0.0 for r in rows]

    return render_template_string(
        CHARTS_HTML,
        username=current_user.id,
        t_ms=t_ms, hip=hipArr, knee=kneeArr, ankle=ankleArr,
        emg=emgArr, emg_rms=emgRmsArr, emg_env=emgEnvArr,
        patient_code=patient_code,
        exercise_name=exercise_name,
        vas_before=vas_before, vas_after=vas_after,
    )



# ===================== HTML =====================
LOGIN_HTML = """
<!doctype html><html lang="vi"><head>
<link rel="icon" type="image/png" href="{{ url_for('static', filename='unnamed.png') }}">
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập IMU</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<style>
:root{
  --card-bg: rgba(5, 10, 25, 0.95);
  --neon-blue: #29d4ff;
  --neon-pink: #ff4fd8;
  --neon-purple: #7b5dff;
}

/* ===== NỀN VŨ TRỤ + LỚP PHỦ ===== */
body{
  min-height:100vh;
  margin:0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;

  background-image: url("{{ url_for('static', filename='space_bg.jpg') }}");
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;

  display:flex;
  align-items:center;
  justify-content:center;
  position:relative;
  overflow:hidden;
}

/* Lớp phủ làm tối + blur nhẹ để neon nổi hơn */
body::before{
  content:"";
  position:fixed;
  inset:0;
  background: radial-gradient(circle at top, rgba(0,0,0,0.25), rgba(0,0,0,0.75));
  backdrop-filter: blur(3px);
  z-index:-2;
}

/* Một chút hạt sao bay mờ mờ */
body::after{
  content:"";
  position:fixed;
  inset:-50px;
  background-image:
    radial-gradient(circle at 10% 20%, rgba(255,255,255,0.12) 0, transparent 35%),
    radial-gradient(circle at 80% 10%, rgba(144,224,255,0.18) 0, transparent 40%),
    radial-gradient(circle at 60% 80%, rgba(255,192,203,0.16) 0, transparent 45%);
  opacity:0.45;
  mix-blend-mode:screen;
  animation: nebulaMove 40s linear infinite;
  z-index:-1;
}

@keyframes nebulaMove{
  0%{ transform:translate3d(0,0,0) scale(1); }
  50%{ transform:translate3d(-30px,10px,0) scale(1.02); }
  100%{ transform:translate3d(0,0,0) scale(1); }
}

/* ===== KHỐI LOGIN NEON ===== */
.login-wrap{
  position:relative;
  padding:3px;
  border-radius:24px;
  background:
    linear-gradient(135deg, rgba(41,212,255,0.9), rgba(255,79,216,0.9));
  box-shadow:
    0 0 35px rgba(41,212,255,0.55),
    0 0 65px rgba(255,79,216,0.5);
  max-width:480px;
  width:100%;
}

/* KHUNG ĐĂNG NHẬP BÊN TRONG */
.login-card{
  position:relative;
  z-index:0;
  border-radius:22px;
  background: radial-gradient(circle at top, #101630 0%, #050a18 55%, #02040b 100%);
  padding:26px 30px 24px;
  color:#e6f3ff;
  box-shadow: 0 22px 60px rgba(0,0,0,0.75) inset;
  overflow:hidden;
}

/* Ô vuông xoay NEON bên trong khung */
.login-card::before,
.login-card::after{
  content:"";
  position:absolute;
  width:230px;
  height:230px;
  border-radius:18px;
  border:1.6px solid rgba(41,212,255,0.35);
  box-shadow:0 0 24px rgba(41,212,255,0.25);
  transform:rotate(25deg);
  animation: spinSquare 22s linear infinite;
  opacity:0.45;
  pointer-events:none;
  z-index:0;
}
.login-card::before{
  top:-90px;
  left:-70px;
}
.login-card::after{
  bottom:-90px;
  right:-80px;
  border-color:rgba(255,79,216,0.45);
  box-shadow:0 0 24px rgba(255,79,216,0.28);
  animation-duration:30s;
}

@keyframes spinSquare{
  0%{ transform:rotate(0deg); }
  100%{ transform:rotate(360deg); }
}

/* LỚP CHỨA NỘI DUNG ĐỂ NỔI TRÊN HÌNH XOAY */
.card-inner{
  position:relative;
  z-index:1;
}

/* Logo & tiêu đề */
.login-logo-row{
  display:flex;
  justify-content:center;
  align-items:center;
  gap:26px;
  margin-bottom:10px;
}
.login-logo{
  width:70px; height:auto;
  filter: drop-shadow(0 0 12px rgba(41,212,255,0.6));
}
.login-title{
  font-size:1.3rem;
  font-weight:800;
  text-align:center;
  letter-spacing:0.08em;
  text-transform:uppercase;
  margin-bottom:4px;
  color:#f7fbff;
  text-shadow:0 0 12px rgba(255,255,255,0.7), 0 0 22px rgba(41,212,255,0.8);
}
.login-subtitle{
  font-size:.85rem;
  text-align:center;
  color:#99c9ff;
  margin-bottom:18px;
}

/* Divider neon mảnh */
.divider{
  height:1px;
  border-radius:999px;
  background:linear-gradient(90deg, transparent, rgba(87,140,255,0.9), transparent);
  box-shadow:0 0 10px rgba(87,140,255,0.9);
  margin-bottom:18px;
}

/* Form */
.form-label{
  font-size:.84rem;
  color:#9dbaf8;
  margin-bottom:4px;
}
.form-control{
  border-radius:999px;
  border:1px solid rgba(90,130,255,0.65);
  background:rgba(5,16,40,0.95);
  color:#e9f2ff;
  font-size:.95rem;
  padding-inline:14px;
  box-shadow:0 0 0 1px rgba(0,0,0,0.45) inset;
}
.form-control::placeholder{ color:#5d76a8; font-size:.85rem; }
.form-control:focus{
  border-color:var(--neon-blue);
  box-shadow:0 0 0 .15rem rgba(41,212,255,0.45);
  background:rgba(3,10,30,1);
  color:#ffffff;
}

/* Nút con mắt */
.btn-eye{
  border-top-right-radius:999px;
  border-bottom-right-radius:999px;
  border-color:rgba(90,130,255,0.8);
  background:linear-gradient(135deg,#07142d,#071d3d);
  color:#a8c7ff;
  font-size:.9rem;
}
.btn-eye:hover{
  background:linear-gradient(135deg,#0b2446,#103263);
  color:#ffffff;
}

/* Buttons */
.btn-primary-neon{
  border-radius:999px;
  border:none;
  font-weight:700;
  font-size:.95rem;
  background:linear-gradient(90deg,#00f0ff,#29b5ff);
  color:#02111f;
  box-shadow:
    0 0 18px rgba(0,240,255,0.75),
    0 0 36px rgba(0,167,255,0.85);
}
.btn-primary-neon:hover{
  filter:brightness(1.1);
  box-shadow:
    0 0 22px rgba(0,240,255,0.9),
    0 0 44px rgba(0,167,255,0.9);
}
.btn-secondary-neon{
  border-radius:999px;
  border:none;
  font-weight:700;
  font-size:.95rem;
  background:linear-gradient(90deg,#ff4fd8,#ff8b7c);
  color:#130014;
  box-shadow:
    0 0 18px rgba(255,79,216,0.75),
    0 0 36px rgba(255,139,124,0.75);
}
.btn-secondary-neon:hover{
  filter:brightness(1.05);
  box-shadow:
    0 0 22px rgba(255,79,216,0.9),
    0 0 44px rgba(255,139,124,0.9);
}

/* Nút về trang giới thiệu */
.btn-outline-ghost{
  border-radius:999px;
  border:1px solid rgba(160,185,255,0.6);
  background:linear-gradient(90deg, rgba(3,10,32,0.9), rgba(5,14,40,0.95));
  color:#c5d8ff;
  font-weight:500;
  font-size:.9rem;
}
.btn-outline-ghost:hover{
  background:linear-gradient(90deg, rgba(6,18,54,0.95), rgba(8,24,70,0.98));
  color:#ffffff;
}

/* Thông báo lỗi */
.error-text{
  font-size:.86rem;
  color:#ff9bb7;
  text-align:center;
  margin-top:6px;
}

/* Đổi màu viền input trong form đăng ký một chút */
#registerForm .form-control{
  border-color:rgba(255,79,216,0.7);
}
#registerForm .form-control:focus{
  border-color:#ff8bd6;
  box-shadow:0 0 0 .15rem rgba(255,139,214,0.55);
}

/* Responsive nhỏ lại một tẹo trên mobile */
@media (max-width:576px){
  .login-card{ padding:22px 18px 20px; }
  .login-title{ font-size:1.1rem; }
}
</style>
</head>

<body>

<div class="login-wrap">
  <div class="login-card">
    <div class="card-inner">

      <!-- LOGO -->
      <div class="login-logo-row">
        <img src="{{ url_for('static', filename='unnamed.png') }}" class="login-logo">
        <img src="{{ url_for('static', filename='retrack.png') }}" class="login-logo">
      </div>

      <div class="login-title">HỆ THỐNG RETRACK</div>
      <div class="login-subtitle">Nền tảng theo dõi & hỗ trợ phục hồi vận động KomLab</div>

      <div class="divider"></div>

      <!-- =================== FORM ĐĂNG NHẬP =================== -->
      <form id="loginForm" method="post" action="/login">
        <div class="mb-3">
          <label class="form-label">Tài khoản</label>
          <input name="username" class="form-control" placeholder="Nhập tài khoản..." required>
        </div>

        <div class="mb-3">
          <label class="form-label">Mật khẩu</label>
          <div class="input-group">
            <input id="loginPassword" name="password" type="password" class="form-control" placeholder="Nhập mật khẩu..." required>
            <button type="button" class="btn btn-eye toggle-password" data-target="loginPassword">👁‍🗨</button>
          </div>
        </div>

        <div class="d-flex gap-2 mt-3">
          <button class="btn btn-primary-neon flex-fill">Đăng nhập</button>
          <button type="button" class="btn btn-secondary-neon flex-fill" id="btnShowRegister">Đăng ký</button>
        </div>

        {% if error_message %}
        <div class="error-text">
            {{ error_message }}
        </div>
        {% endif %}
      </form>

      <!-- =================== FORM ĐĂNG KÝ =================== -->
      <form id="registerForm" method="post" action="/register" style="display:none; margin-top:4px;">
        <div class="mb-2 text-center fw-semibold" style="color:#ffd3ff;">Tạo tài khoản mới</div>

        <div class="mb-3">
          <label class="form-label">Tài khoản</label>
          <input name="reg_username" class="form-control" placeholder="" required>
        </div>

        <div class="mb-3">
          <label class="form-label">Mật khẩu</label>
          <div class="input-group">
            <input id="regPassword" name="reg_password" type="password" class="form-control" required>
            <button type="button" class="btn btn-eye toggle-password" data-target="regPassword">👁‍🗨</button>
          </div>
        </div>

        <div class="mb-3">
          <label class="form-label">Nhập lại mật khẩu</label>
          <div class="input-group">
            <input id="regPassword2" name="reg_password2" type="password" class="form-control" required>
            <button type="button" class="btn btn-eye toggle-password" data-target="regPassword2">👁‍🗨</button>
          </div>

          <div id="pwError" class="error-text" style="display:none;">
             Mật khẩu không khớp
          </div>
        </div>

        <div class="d-flex gap-2 mt-2">
          <button type="submit" class="btn btn-secondary-neon flex-fill">Đăng ký</button>
          <button type="button" class="btn btn-outline-ghost flex-fill" id="btnShowLogin">← Đăng nhập</button>
        </div>
      </form>

      <hr class="mt-4 mb-3" style="border-color:rgba(110,140,255,0.5);">

      <a class="btn btn-outline-ghost w-100" href="https://sites.google.com/view/biotrackers/trang-ch%E1%BB%A7?authuser=2">← Về chúng tôi</a>

    </div>
  </div>
</div>

<!-- =================== SCRIPT =================== -->
<script>
  const loginForm    = document.getElementById('loginForm');
  const registerForm = document.getElementById('registerForm');
  const btnShowReg   = document.getElementById('btnShowRegister');
  const btnShowLogin = document.getElementById('btnShowLogin');

  // Chuyển qua form đăng ký
  btnShowReg.addEventListener('click', () => {
      loginForm.style.display = 'none';
      registerForm.style.display = 'block';
  });

  // Quay lại đăng nhập
  btnShowLogin.addEventListener('click', () => {
      registerForm.style.display = 'none';
      loginForm.style.display = 'block';
  });

  // Toggle hiển thị mật khẩu
  document.querySelectorAll('.toggle-password').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.target);
      const isHidden = target.type === 'password';
      target.type = isHidden ? 'text' : 'password';
      btn.textContent = isHidden ? "🔒" : "👁‍";
    });
  });

  // Kiểm tra mật khẩu trùng nhau trong form đăng ký
  const pw1 = document.getElementById('regPassword');
  const pw2 = document.getElementById('regPassword2');
  const pwError = document.getElementById('pwError');

  function checkPw() {
    if (!pw1.value || !pw2.value) {
        pwError.style.display = "none";
        return;
    }
    pwError.style.display = pw1.value !== pw2.value ? "block" : "none";
  }

  pw1.addEventListener("input", checkPw);
  pw2.addEventListener("input", checkPw);

  registerForm.addEventListener("submit", e => {
    checkPw();
    if (pwError.style.display === "block") e.preventDefault();
  });
</script>

</body></html>
"""
CALIBRATION_HTML = """ 
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title data-i18n="calib.page_title">Hiệu chuẩn</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<style>
:root{ --blue:#1669c9; --sbw:260px; }

body{
  background:#e8f3ff;
  margin:0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.layout{
  display:flex;
  gap:16px;
  position:relative;
}
.sidebar{
  background:var(--blue);
  color:#fff;
  border-top-right-radius:16px;
  border-bottom-right-radius:16px;
  padding:16px;
  width:var(--sbw);
  min-height:100vh;
  box-sizing:border-box;
}
.sidebar-col{
  flex:0 0 var(--sbw);
  max-width:var(--sbw);
  transition:flex-basis .28s ease, max-width .28s ease, transform .28s ease;
}
.main-col{
  flex:1 1 auto;
  min-width:0;
}

/* collapsed sidebar */
.sb-collapsed .sidebar-col{
  flex-basis:0;
  max-width:0;
  transform:translateX(-8px);
}
.sb-collapsed .sidebar{
  padding:0;
  width:0;
  border-radius:0;
}
.sb-collapsed .sidebar *{
  display:none;
}

/* Navbar toggle button */
#btnToggleSB{
  border:2px solid #d8e6ff;
  border-radius:10px;
  background:#fff;
  padding:6px 10px;
  font-weight:700;
}
#btnToggleSB:hover{ background:#f4f8ff; }

/* Menu buttons */
.menu-btn{
  width:100%;
  display:block;
  background:#1973d4;
  border:none;
  color:#fff;
  padding:10px 12px;
  margin:8px 0;
  border-radius:12px;
  font-weight:600;
  text-align:left;
  text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; }
.menu-btn.active{ background:#0f5bb0; }

/* Video block */
.video-card{
  background:#ffffff;
  border-radius:18px;
  box-shadow:0 10px 30px rgba(15,23,42,.16);
  padding:18px 18px 22px;
  max-width:1100px;
  margin:24px auto 32px auto;
}
.video-title{
  font-weight:700;
  color:#0a3768;
  margin-bottom:12px;
}
.video-frame video{
  width:100%;
  height:100%;
  border-radius:16px;
}
</style>
</head>

<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>

    <span class="navbar-brand mb-0">
      <span data-i18n="nav.hello">Xin chào,</span> {{username}}
    </span>

    <div class="ms-auto d-flex align-items-center gap-2">
      <img src="{{ url_for('static', filename='unnamed.png') }}"
           alt="Logo" height="40">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">

    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>

        <a class="menu-btn" href="/"                data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn active" href="/calibration" data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage" data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/records"        data-i18n="menu.records">Bệnh án</a>
        <a class="menu-btn" href="/charts"         data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings"       data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main content -->
    <main class="main-col">
      <div class="video-card">
        <div class="video-title" data-i18n="calib.title">HƯỚNG DẪN HIỆU CHUẨN IMU</div>

        <div class="video-frame ratio ratio-16x9">
          <video autoplay loop muted controls playsinline>
            <source src="{{ url_for('static', filename='videos/calibration_loop.mp4') }}" type="video/mp4">
          </video>
        </div>
      </div>
    </main>

  </div>
</div>

<script>
// ============= I18N DICTIONARY =============
const I18N = {
  vi: {
    "nav.hello": "Xin chào,",
    "menu.title": "MENU",
    "menu.home": "Trang chủ",
    "menu.calib": "Hiệu chuẩn",
    "menu.patinfo": "Thông tin bệnh nhân",
    "menu.records": "Bệnh án",
    "menu.charts": "Biểu đồ",
    "menu.settings": "Cài đặt",

    "calib.page_title": "Hiệu chuẩn",
    "calib.title": "HƯỚNG DẪN HIỆU CHUẨN IMU",
  },
  en: {
    "nav.hello": "Hello,",
    "menu.title": "MENU",
    "menu.home": "Home",
    "menu.calib": "Calibration",
    "menu.patinfo": "Patient info",
    "menu.records": "Records",
    "menu.charts": "Charts",
    "menu.settings": "Settings",

    "calib.page_title": "Calibration",
    "calib.title": "IMU CALIBRATION GUIDE",
  }
};

// ============= Apply language =============
function applyLanguage(lang){
  const dict = I18N[lang] || I18N.vi;

  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k = el.getAttribute("data-i18n");
    if (dict[k]) el.textContent = dict[k];
  });

  const titleEl = document.querySelector("title[data-i18n]");
  if (titleEl){
    const key = titleEl.getAttribute("data-i18n");
    if (dict[key]) titleEl.textContent = dict[key];
  }
}

// ============= Sidebar toggle =============
document.getElementById('btnToggleSB').onclick = () => {
  document.body.classList.toggle('sb-collapsed');
};

// ============= Load language at startup =============
document.addEventListener("DOMContentLoaded", ()=>{
  const lang = localStorage.getItem("appLang") || "vi";
  applyLanguage(lang);
});
</script>

</body>
</html>
"""

RECORD_HTML = """
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bệnh án điện tử</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<style>
:root { --blue:#1669c9; --sbw:260px; }
body{
  background:#e8f3ff;
  margin:0;
  font-size:15px;
}
.layout{ display:flex; gap:16px; position:relative; }

.sidebar-col{
  flex:0 0 var(--sbw);
  max-width:var(--sbw);
  transition:all .28s ease;
}
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px;
  border-bottom-right-radius:16px;
  padding:16px;
  min-height:100vh;
}
.main-col{ flex:1 1 auto; min-width:0; }

body.sb-collapsed .sidebar-col{
  flex-basis:0 !important;
  max-width:0 !important;
}
body.sb-collapsed .sidebar{
  padding:0 !important;
}
body.sb-collapsed .sidebar *{
  display:none;
}

#btnToggleSB{
  border:2px solid #d8e6ff;
  background:#fff;
  border-radius:10px;
  padding:6px 10px;
  font-weight:700;
}
#btnToggleSB:hover{ background:#eef6ff; }

.menu-btn{
  width:100%;
  display:block;
  background:#1d74d8;
  border:none;
  color:#fff;
  padding:10px 12px;
  margin:8px 0;
  border-radius:12px;
  font-weight:600;
  text-align:left;
  text-decoration:none;
}
.menu-btn:hover{ background:#1f5bb0; }
.menu-btn.active{ background:#0f5bb0; }

.panel{
  background:#fff;
  border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,0.10);
  padding:16px;
  margin-bottom:16px;
}
.badge-pill{
  border-radius:999px;
}
</style>
</head>

<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>

    <div class="ms-auto d-flex align-items-center gap-3">
      <img src="/static/unnamed.png" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">

    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold">MENU</div>
        <a class="menu-btn" href="/">Trang chủ</a>
        <a class="menu-btn" href="/calibration">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage">Thông tin bệnh nhân</a>
        <a class="menu-btn active" href="/records">Bệnh án</a>
        <a class="menu-btn" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <main class="main-col">
      <div class="panel mb-3">
        <h5 class="mb-1">Danh sách bệnh án</h5>
        <div class="text-muted small">
          Các bản ghi được lưu khi nhấn nút <strong>"Lưu kết quả"</strong> trên trang đo.
        </div>

        <div class="mt-2">
          <input id="recordSearch" class="form-control form-control-sm"
                 placeholder="Tìm theo tên, mã bệnh nhân, ngày đo...">
        </div>
      </div>

      <div class="panel">
        {% if records %}
        <div class="table-responsive">
          <table class="table table-hover align-middle mb-0" id="recordsTable">
            <thead class="table-light">
              <tr>
                <th>#</th>
                <th>Thời gian lưu</th>
                <th>Mã BN</th>
                <th>Họ và tên</th>
                <th>Ngày đo</th>
                <th>Bài tập &amp; điểm</th>
                <th>Mức đau (VAS)</th>
                <th style="width:80px;">Thao tác</th>  <!-- cột Xóa -->
              </tr>
            </thead>
            <tbody>
              {% for r in records %}
              {% set info   = r.patient_info or {} %}
              {% set scores = r.exercise_scores or {} %}
              {% set vas    = r.vas_summary or {} %}
              <tr>
                <td>{{ loop.index }}</td>
                <td class="small text-muted">{{ r.created_at }}</td>
                <td>{{ r.patient_code or "—" }}</td>
                <td>{{ info.name or "—" }}</td>
                <td>{{ r.measure_date or "—" }}</td>

                <!-- Bài tập & điểm -->
                <td>
                  {% if scores %}
                    {% for ex_name, ex in scores.items() %}
                      <div class="small">
                        <strong>{{ ex_name }}</strong>:
                        ROM Knee {{ '%.1f'|format(ex.romKnee or 0) }}°
                        – điểm {{ ex.score or 0 }}/2
                      </div>
                    {% endfor %}
                  {% else %}
                    <span class="text-muted small">Chưa có điểm bài tập.</span>
                  {% endif %}
                </td>

                <!-- Mức đau (VAS) -->
                <td>
                  {% if vas %}
                    {% for ex_name, v in vas.items() %}
                      {% set vb = v.before if v.before is not none else None %}
                      {% set va = v.after  if v.after  is not none else None %}
                      <div class="small">
                        <strong>{{ ex_name }}</strong>:
                        {% if vb is not none %}
                          {{ vb }}/10
                        {% else %}—{% endif %}
                        →
                        {% if va is not none %}
                          {{ va }}/10
                        {% else %}—{% endif %}
                      </div>
                    {% endfor %}
                  {% else %}
                    <span class="text-muted small">Chưa ghi nhận.</span>
                  {% endif %}
                </td>

                <!-- Nút xóa -->
                <td>
                  <button type="button"
                          class="btn btn-sm btn-outline-danger"
                          onclick="deleteRecord({{ loop.index0 }})">
                    Xóa
                  </button>
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        {% else %}
          <div class="text-muted small">Chưa có bệnh án nào được lưu.</div>
        {% endif %}
      </div>
    </main>

  </div>
</div>

<script>
document.getElementById("btnToggleSB").onclick = () =>
  document.body.classList.toggle("sb-collapsed");

// bộ lọc đơn giản
const inp = document.getElementById("recordSearch");
if (inp){
  inp.addEventListener("input", () => {
    const kw = inp.value.toLowerCase();
    document.querySelectorAll("#recordsTable tbody tr").forEach(tr => {
      tr.style.display = tr.innerText.toLowerCase().includes(kw) ? "" : "none";
    });
  });
}

// hàm xóa bản ghi
function deleteRecord(index) {
  if (!confirm("Bạn có chắc muốn xóa bản ghi này?")) return;

  fetch("/api/delete_record", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ index })
  })
  .then(r => r.json())
  .then(res => {
    if (res.ok) {
      alert("Đã xóa bản ghi.");
      location.reload();
    } else {
      alert("Lỗi: " + (res.msg || "Không xóa được."));
    }
  })
  .catch(err => {
    console.error(err);
    alert("Lỗi kết nối khi xóa bản ghi.");
  });
}
</script>

</body>
</html>
"""


PATIENT_NEW_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thêm bệnh nhân mới</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body{
  background:#e8f3ff;
}

.card{
  border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,.06);
}
.btn-outline-thick{
  border:2px solid #151515;
  border-radius:12px;
  background:#fff;
  font-weight:600;
}
.form-label{
  font-weight:600;
  color:#274b6d;
}
</style>
</head>
<body>

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid">
    <span class="navbar-brand">Thêm bệnh nhân mới</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <a class="btn btn-outline-secondary" href="/">← Trang chủ</a>
      <img src="{{ url_for('static', filename='unnamed.png') }}" height="40">
    </div>
  </div>
</nav>

<div class="container my-3" style="max-width:720px">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for c,m in messages %}
      <div class="alert alert-{{c}}">{{m}}</div>
    {% endfor %}
  {% endwith %}
  <div class="card p-4">
    <form method="post">
      <div class="mb-3">
        <label class="form-label">Họ và tên</label>
        <input name="full_name" class="form-control" required>
      </div>
      <div class="mb-3">
        <label class="form-label">CCCD</label>
        <input name="national_id" class="form-control">
      </div>
      <div class="row g-3">
        <div class="col-md-6">
          <label class="form-label">Ngày sinh</label>
          <input type="text" name="dob" class="form-control" placeholder="vd 30/05/2001 hoặc 2001-05-30">
        </div>
        <div class="col-md-6">
          <label class="form-label">Giới tính</label>
          <select name="sex" class="form-select">
            <option value="">--</option>
            <option>Male</option>
            <option>Female</option>
          </select>
        </div>
      </div>
      <div class="row g-3 mt-0">
        <div class="col-md-6">
          <label class="form-label">Cân nặng (kg)</label>
          <input name="weight" class="form-control">
        </div>
        <div class="col-md-6">
          <label class="form-label">Chiều cao (cm)</label>
          <input name="height" class="form-control">
        </div>
      </div>
      <div class="mt-4 d-grid">
        <button class="btn btn-outline-thick py-2">Lưu thông tin</button>
      </div>
    </form>
  </div>
</div>
</body></html>
"""


# ======= Dashboard (sidebar ẩn, bấm ☰ để mở) =======
DASH_HTML = """<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMU Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<script type="importmap">
{ "imports": { "three": "https://unpkg.com/three@0.154.0/build/three.module.js" } }
</script>

<style>
:root{ --blue:#1669c9; --soft:#f3f7ff; --sbw:260px; --video-h:360px; }
body{ background:#fafbfe }
.layout{ display:flex; gap:16px; position:relative;overflow-x:hidden; }

/* Sidebar */
.sidebar{ background:var(--blue); color:#fff; border-top-right-radius:16px; border-bottom-right-radius:16px; padding:16px; width:var(--sbw); min-height:100%; box-sizing:border-box; }
.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); transition:flex-basis .28s ease, max-width .28s ease, transform .28s ease; will-change:flex-basis,max-width,transform; }
.main-col{ flex:1 1 auto; min-width:0; }

/* ===== CSS VAS ===== */
.vas-wrapper { padding: 16px 18px; background: #ffffff; border-radius: 12px; box-shadow: 0 3px 10px rgba(0,0,0,0.08); }
.vas-line-container { position: relative; width: 100%; }
.vas-line { height: 3px; background: #0097b2; margin-bottom: 26px; }
.vas-ticks { display: flex; justify-content: space-between; position: relative; margin-top: -18px; }
.vas-tick { position: relative; font-size: 14px; color: #222; cursor: pointer; text-align: center; }
.vas-tick::before { content: ""; width: 2px; height: 18px; background: #0097b2; display: block; margin: 0 auto 4px auto; }
.vas-tick.active::before { background: #ff5722; height: 24px; }
.vas-tick.active { color: #ff5722; font-weight: 600; }
.vas-labels { display: flex; justify-content: space-between; margin-top: 24px; font-size: 11px; color: #444; text-align: center; }
.vas-labels span small { font-size: 10px; }

/* Thu gọn mặc định */
.sb-collapsed .sidebar-col{ flex-basis:0; max-width:0; transform:translateX(-8px); }
.sb-collapsed .sidebar{ padding:0; width:0; border-radius:0; }
.sb-collapsed .sidebar *{ display:none; }

.panel{ background:#fff; border-radius:16px; box-shadow:0 8px 20px rgba(16,24,40,.06); padding:16px;overflow:hidden; }
.title-chip{ display:inline-block; background:#e6f2ff; border:2px solid #9ccaff; color:#073c74; padding:8px 14px; border-radius:14px; font-weight:800; }
.table thead th{ background:#eef5ff; color:#083a6a }
.btn-outline-thick{ border:2px solid #151515; border-radius:12px; background:#fff; font-weight:700; }
.form-label{ font-weight:600; color:#244e78 }
.compact .row.g-3{ --bs-gutter-x:1rem; --bs-gutter-y:1rem; }
.compact .btn-outline-thick{ padding:10px 12px; border-radius:10px; }
#guideVideo{ height:var(--video-h); border-radius:14px; background:#000; }
@media (min-width:1400px){ :root{ --video-h:400px; } }
@media (min-width:992px){ .pull-up-guide{ margin-top:-318px !important; } }

#btnToggleSB{ border:2px solid #d8e6ff; border-radius:10px; background:#fff; padding:6px 10px; font-weight:700; }
#btnToggleSB:hover{ background:#f4f8ff; }

.menu-btn{ width:100%; display:block; background:#1973d4; border:none; color:#fff; padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600; text-align:left; text-decoration:none; }
.menu-btn:hover{ background:#1f80ea; color:#fff }

/* nền khung three: xanh nhạt; muốn trắng đổi thành #ffffff */
#threeMount{ background:#eaf2ff; }

/* ===================== EMG UI (đẹp + dễ quan sát) ===================== */
.emg-card{
  background: linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
  border: 1px solid #e6eefc;
  border-radius: 16px;
  padding: 14px 14px 12px;
  box-shadow: 0 10px 24px rgba(16,24,40,.06);
}
.emg-hr{ width:210px; }
.emg-hr .input-group-text,
.emg-hr .form-control{
  border-color:#dbe7ff;
  font-weight:800;
}
.emg-hr .form-control{ text-align:center; }

.emg-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  margin-bottom:10px;
}
.emg-title{
  display:flex;
  align-items:center;
  gap:10px;
}
.emg-dot{
  width:10px;height:10px;border-radius:50%;
  background:#20c997;
  box-shadow:0 0 0 6px rgba(32,201,151,.14);
}
.emg-title .title-chip{ margin:0; }
.emg-meta{
  display:flex;
  align-items:center;
  gap:8px;
  flex-wrap:wrap;
  justify-content:flex-end;
}
.emg-pill{
  border:1px solid #dde7fb;
  background:#f7fbff;
  border-radius:999px;
  padding:6px 10px;
  font-weight:700;
  font-size:12px;
  color:#0b2d52;
  line-height:1;
  white-space:nowrap;
}
.emg-pill strong{ font-size:13px; }
.emg-rms{
  border:1px solid #cfe6ff;
  background:#eaf4ff;
  color:#073c74;
}
.emg-rms strong{ font-size:14px; }

.emg-plot{
  position:relative;
  height:210px;
  border-radius:14px;
  overflow:hidden;
  border:1px solid #e6eefc;
  background:
    linear-gradient(180deg, rgba(22,105,201,.08) 0%, rgba(22,105,201,0) 55%),
    repeating-linear-gradient(0deg, rgba(8,58,106,.07) 0, rgba(8,58,106,.07) 1px, transparent 1px, transparent 28px),
    repeating-linear-gradient(90deg, rgba(8,58,106,.05) 0, rgba(8,58,106,.05) 1px, transparent 1px, transparent 48px),
    #ffffff;
}
.emg-plot:before{
  content:"µV";
  position:absolute;
  top:10px; left:12px;
  font-size:12px;
  font-weight:800;
  color:#0b2d52;
  opacity:.75;
  padding:2px 8px;
  border-radius:999px;
  background:#f3f8ff;
  border:1px solid #e2ecff;
  z-index:2;
}
#emgChart{ width:100%; height:100%; display:block; }

.emg-controls{
  margin-top:10px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:10px;
  flex-wrap:wrap;
}
.emg-controls .input-group .input-group-text{
  font-weight:800;
  background:#f3f8ff;
  border-color:#dbe7ff;
  color:#073c74;
}
.emg-controls .form-select,
.emg-controls .form-control{
  border-color:#dbe7ff;
  font-weight:700;
}
.emg-hint{
  margin-top:8px;
  color:#5b6b7c;
  font-size:12px;
}

/* Mobile: plot cao hơn chút */
@media (max-width:576px){
  .emg-plot{ height:260px; }
}

#status3D{ display:none; }

/* Khi không phải knee -> ẩn card EMG */
.emg-hidden{ display:none !important; }
</style>
</head>

<body class="compact sb-collapsed">
<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>
        <a class="menu-btn" href="/" data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn" href="/calibration" data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage" data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/patients" data-i18n="menu.review">Xem lại</a>
        <a class="menu-btn" href="/records" data-i18n="menu.record">Bệnh án</a>
        <a class="menu-btn" href="/charts" data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings" data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="row g-3">

        <div class="col-lg-7">
          <div class="panel mb-3">
            <div class="d-flex gap-2">
              <a class="btn btn-outline-thick flex-fill" href="#" id="btnPatientList" data-i18n="dash.patient_list">Danh sách bệnh nhân</a>
              <a class="btn btn-outline-thick flex-fill" href="/patients/new" data-i18n="dash.add_patient">Thêm bệnh nhân mới</a>
            </div>

            <!-- ✅ EMG WAVEFORM (chỉ hiện khi chọn KNEE) -->
            <div class="mt-3 emg-card" id="emgCard">
              <div class="emg-head">
                <div class="emg-title">
                  <span class="emg-dot" title="Live"></span>
                  <span class="title-chip" data-i18n="dash.emg_title">EMG</span>
                </div>
                <div class="emg-meta">
                  <div class="input-group input-group-sm emg-hr">
                    <span class="input-group-text" data-i18n="dash.heart_label">Nhịp tim :</span>
                    <input class="form-control" id="heartRate" inputmode="numeric" placeholder="--">
                    <span class="input-group-text" data-i18n="dash.heart_unit">bpm</span>
                  </div>

                  <span class="emg-pill">Sensor <strong>5</strong> (Knee)</span>
                  <span class="emg-pill">Scale <strong id="emgScaleTxt">±1000</strong> µV</span>
                  <span class="emg-pill">Window <strong id="emgWinTxt">5</strong>s</span>
                  <span class="emg-pill emg-rms">RMS <strong id="emgRmsTxt">--</strong> µV</span>
                </div>

              </div>

              <div class="emg-plot">
                <canvas id="emgChart"></canvas>
              </div>

              <div class="emg-controls">
                <div class="input-group" style="max-width:260px;">
                  <span class="input-group-text">Kênh</span>
                  <select class="form-select" id="emgChannelSel" disabled>
                    <option value="emg" selected>emg (sensor 5)</option>
                  </select>
                </div>

                <div class="input-group" style="max-width:220px;">
                  <span class="input-group-text" data-i18n="emg.window">Window</span>
                  <input class="form-control" id="emgWin" type="number" min="1" max="20" step="1" value="5">
                  <span class="input-group-text" data-i18n="unit.second">s</span>
                </div>

                <div class="input-group" style="max-width:240px;">
                  <span class="input-group-text">Scale ±</span>
                  <input class="form-control" id="emgScale" type="number" min="50" max="20000" step="50" value="1000">
                  <span class="input-group-text">µV</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="col-lg-5">
          <div class="panel mb-3">
            <div class="row g-2">
              <div class="col-6">
                <label class="form-label" data-i18n="pat.name">Họ và tên :</label>
                <input id="pat_name" class="form-control">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="pat.dob">Ngày sinh :</label>
                <input id="pat_dob" type="date" class="form-control">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="pat.id">CCCD :</label>
                <input id="pat_cccd" class="form-control">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="pat.gender">Giới tính :</label>
                <input id="pat_gender" class="form-control">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="pat.weight">Cân nặng :</label>
                <input id="pat_weight" class="form-control">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="pat.height">Chiều cao :</label>
                <input id="pat_height" class="form-control">
              </div>

              <input type="hidden" id="pat_code">

              <div class="col-8">
                <label class="form-label" data-i18n="pat.exercise">Bài kiểm tra :</label>
                <div class="input-group">
                  <select class="form-select" id="exerciseSelect">
                    <option value="ankle flexion">ankle flexion</option>
                    <option value="knee flexion">knee flexion</option>
                    <option value="hip flexion">hip flexion</option>
                  </select>
                  <button class="btn btn-outline-thick" type="button" id="btnAddExercise">+</button>
                </div>
              </div>
              <div class="col-4">
                <label class="form-label" data-i18n="pat.measure_date">Ngày đo :</label>
                <input id="measure_date" type="date" class="form-control">
              </div>
            </div>
          </div>

          <video id="guideVideo" class="w-100" controls playsinline preload="metadata" poster="">
            Sorry, your browser doesn’t support embedded videos.
          </video>
        </div>

        <!-- MÔ PHỎNG 3D -->
        <div class="col-lg-7 pull-up-guide">
          <div class="panel">
            <div class="d-flex align-items-center justify-content-between mb-2">
              <span class="title-chip" data-i18n="dash.3d_title">MÔ PHỎNG 3D</span>
              <div class="small text-muted" data-i18n="dash.3d_source">Nguồn: hip/knee/ankle từ IMU (độ)</div>
            </div>

            <div id="threeMount" style="width:100%; height:480px; min-height:480px; border-radius:14px; overflow:visible; position:relative; z-index:1;"></div>

            <div class="text-center mt-2">
              <span class="badge text-bg-light border me-2">Hip: <span id="liveHip">--</span>°</span>
              <span class="badge text-bg-light border me-2">Knee: <span id="liveKnee">--</span>°</span>
              <span class="badge text-bg-light border">Ankle: <span id="liveAnkle">--</span>°</span>
            </div>

            <div class="mt-3 text-center">
              <button class="btn btn-outline-thick px-4 py-2" id="btnResetPose3D" data-i18n="dash.reset3d">Reset 3D</button>
              <div class="small text-muted mt-2" id="status3D"> Đang khởi tạo 3D… </div>
            </div>
          </div>
        </div>

        <!-- NÚT + KẾT QUẢ -->
        <div class="col-lg-5">
          <div class="panel d-grid gap-3">
            <button class="btn btn-outline-thick py-3" id="btnStart" data-i18n="dash.start_measure">Bắt đầu đo</button>
            <button class="btn btn-outline-thick py-3" id="btnStop" data-i18n="dash.stop_measure">Kết thúc đo</button>
            <button class="btn btn-outline-thick py-3" id="btnSave" data-i18n="dash.save_result">Lưu kết quả</button>

            <div id="exercise-result-panel" class="mt-3" style="display:none;">
              <h6 id="exercise-title-text" class="fw-bold mb-2"></h6>
              <div style="height:160px;">
                <canvas id="exercise-chart"></canvas>
              </div>
              <div class="mt-2 small">
                <div data-i18n="dash.rom_hip">ROM Hip: <span id="rom-hip-text">0°</span></div>
                <div data-i18n="dash.rom_knee">ROM Knee: <span id="rom-knee-text">0°</span></div>
                <div data-i18n="dash.rom_ankle">ROM Ankle: <span id="rom-ankle-text">0°</span></div>
                <div class="mt-1 fw-bold">
                  <span data-i18n="dash.score_this_ex">Điểm bài này:</span>
                  <span id="score-text">0</span> / 2
                </div>
              </div>
              <div class="mt-3 d-flex gap-2">
                <button id="btn-next-ex" class="btn btn-outline-thick flex-grow-1" data-i18n="dash.next_ex"> Bài tập tiếp theo </button>
              </div>
            </div>

            <div id="all-exercise-summary" class="mt-3" style="display:none;">
              <h6 class="fw-bold" data-i18n="dash.summary_all">Tổng kết tất cả bài tập</h6>
              <ul class="small mb-2" id="summary-list"></ul>
              <div class="fw-bold">
                <span data-i18n="dash.total_score">Tổng điểm:</span>
                <span id="total-score-text">0</span>
              </div>
            </div>

          </div>
        </div>

      </div>
    </main>
  </div>
</div>

<!-- Modal chọn bệnh nhân -->
<div class="modal fade" id="patientModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" data-i18n="pat.modal_title">Danh sách bệnh nhân</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <input id="pm_search" class="form-control mb-2" placeholder="Tìm kiếm..." data-i18n-placeholder="pat.modal_search">
        <div class="table-responsive" style="max-height:400px;">
          <table class="table table-hover align-middle mb-0">
            <thead>
              <tr>
                <th data-i18n="pat.th_index">#</th>
                <th data-i18n="pat.th_code">Mã</th>
                <th data-i18n="pat.th_name">Họ và tên</th>
                <th data-i18n="pat.th_cccd">CCCD</th>
                <th data-i18n="pat.th_dob">Ngày sinh</th>
                <th data-i18n="pat.th_gender">Giới tính</th>
              </tr>
            </thead>
            <tbody id="pm_body"></tbody>
          </table>
        </div>
        <div class="small text-muted mt-2" data-i18n="pat.modal_hint">Nhấp đúp vào 1 dòng để chọn bệnh nhân.</div>
      </div>
    </div>
  </div>
</div>

<!-- Modal VAS dùng chung -->
<div class="modal fade" id="vasModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog modal-lg modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="vasModalTitle">Đánh giá mức độ đau (VAS)</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Đóng"></button>
      </div>
      <div class="modal-body">
        <p class="text-muted mb-3" id="vasModalSubtitle">
          Vui lòng chọn mức độ đau từ 0 (không đau) đến 10 (đau tệ nhất).
        </p>

        <div class="vas-wrapper">
          <div class="vas-line-container">
            <div class="vas-line"></div>
            <div class="vas-ticks">
              <div class="vas-tick" data-value="0" onclick="selectVASTick(0)">0</div>
              <div class="vas-tick" data-value="1" onclick="selectVASTick(1)">1</div>
              <div class="vas-tick" data-value="2" onclick="selectVASTick(2)">2</div>
              <div class="vas-tick" data-value="3" onclick="selectVASTick(3)">3</div>
              <div class="vas-tick" data-value="4" onclick="selectVASTick(4)">4</div>
              <div class="vas-tick" data-value="5" onclick="selectVASTick(5)">5</div>
              <div class="vas-tick" data-value="6" onclick="selectVASTick(6)">6</div>
              <div class="vas-tick" data-value="7" onclick="selectVASTick(7)">7</div>
              <div class="vas-tick" data-value="8" onclick="selectVASTick(8)">8</div>
              <div class="vas-tick" data-value="9" onclick="selectVASTick(9)">9</div>
              <div class="vas-tick" data-value="10" onclick="selectVASTick(10)">10</div>
            </div>

            <div class="vas-labels">
              <span>0<br><small>Không đau</small></span>
              <span>1–2<br><small>Đau rất nhẹ</small></span>
              <span>3–4<br><small>Đau nhẹ đến<br>trung bình</small></span>
              <span>5–6<br><small>Đau trung bình<br>đến nhiều</small></span>
              <span>7–8<br><small>Đau nhiều</small></span>
              <span>9–10<br><small>Đau rất nhiều /<br>tệ nhất</small></span>
            </div>
          </div>

          <div class="mt-3">
            <strong>Mức đau bạn chọn: </strong>
            <span id="vasSelected">0 – Không đau</span>
          </div>
        </div>
      </div>

      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Hủy</button>
        <button type="button" class="btn btn-primary" id="vasConfirmBtn"> Xác nhận mức đau </button>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

<!-- ===== SIMPLE I18N (VI / EN) ===== -->
<script>
const I18N = {
  vi: {
    "menu.title": "MENU",
    "menu.home": "Trang chủ",
    "menu.calib": "Hiệu chuẩn",
    "menu.patinfo": "Thông tin bệnh nhân",
    "menu.review": "Xem lại",
    "menu.record": "Bệnh án",
    "menu.charts": "Biểu đồ",
    "menu.settings": "Cài đặt",
    "emg.window": "Cửa sổ",
    "unit.second": "s",

    "dash.patient_list": "Danh sách bệnh nhân",
    "dash.add_patient": "Thêm bệnh nhân mới",
    "dash.heart_label": "Nhịp tim :",
    "dash.heart_unit": "bpm",

    "dash.emg_title": "EMG",

    "dash.3d_title": "MÔ PHỎNG 3D",
    "dash.3d_source": "Nguồn: hip/knee/ankle từ IMU (độ)",
    "dash.reset3d": "Reset 3D",

    "dash.start_measure": "Bắt đầu đo",
    "dash.stop_measure": "Kết thúc đo",
    "dash.save_result": "Lưu kết quả",

    "dash.rom_hip": "ROM Hip:",
    "dash.rom_knee": "ROM Knee:",
    "dash.rom_ankle": "ROM Ankle:",
    "dash.score_this_ex": "Điểm bài này:",
    "dash.next_ex": "Bài tập tiếp theo",
    "dash.summary_all": "Tổng kết tất cả bài tập",
    "dash.total_score": "Tổng điểm:",

    "pat.name": "Họ và tên :",
    "pat.dob": "Ngày sinh :",
    "pat.id": "CCCD :",
    "pat.gender": "Giới tính :",
    "pat.weight": "Cân nặng :",
    "pat.height": "Chiều cao :",
    "pat.exercise": "Bài kiểm tra :",
    "pat.measure_date": "Ngày đo :",

    "pat.modal_title": "Danh sách bệnh nhân",
    "pat.modal_search": "Tìm kiếm...",
    "pat.th_index": "#",
    "pat.th_code": "Mã",
    "pat.th_name": "Họ và tên",
    "pat.th_cccd": "CCCD",
    "pat.th_dob": "Ngày sinh",
    "pat.th_gender": "Giới tính",
    "pat.modal_hint": "Nhấp đúp vào 1 dòng để chọn bệnh nhân."
  },
  en: {
    "menu.title": "MENU",
    "menu.home": "Home",
    "menu.calib": "Calibration",
    "menu.patinfo": "Patient info",
    "menu.review": "Review",
    "menu.record": "Medical record",
    "menu.charts": "Charts",
    "menu.settings": "Settings",
    "emg.window": "Window",
    "unit.second": "s",

    "dash.patient_list": "Patient list",
    "dash.add_patient": "Add new patient",
    "dash.heart_label": "Heart rate:",
    "dash.heart_unit": "bpm",

    "dash.emg_title": "EMG",

    "dash.3d_title": "3D Simulation",
    "dash.3d_source": "Source: hip/knee/ankle from IMU (deg)",
    "dash.reset3d": "Reset 3D",

    "dash.start_measure": "Start measurement",
    "dash.stop_measure": "Stop measurement",
    "dash.save_result": "Save results",

    "dash.rom_hip": "ROM Hip:",
    "dash.rom_knee": "ROM Knee:",
    "dash.rom_ankle": "ROM Ankle:",
    "dash.score_this_ex": "Score for this exercise:",
    "dash.next_ex": "Next exercise",
    "dash.summary_all": "Summary of all exercises",
    "dash.total_score": "Total score:",

    "pat.name": "Full name:",
    "pat.dob": "Date of birth:",
    "pat.id": "National ID:",
    "pat.gender": "Gender:",
    "pat.weight": "Weight:",
    "pat.height": "Height:",
    "pat.exercise": "Exercise:",
    "pat.measure_date": "Measurement date:",

    "pat.modal_title": "Patient list",
    "pat.modal_search": "Search...",
    "pat.th_index": "#",
    "pat.th_code": "Code",
    "pat.th_name": "Full name",
    "pat.th_cccd": "National ID",
    "pat.th_dob": "Date of birth",
    "pat.th_gender": "Gender",
    "pat.modal_hint": "Double-click a row to select patient."
  }
};

function applyLanguage(lang){
  const dict = I18N[lang] || I18N.vi;
  document.querySelectorAll("[data-i18n]").forEach(el => {
    const key = el.getAttribute("data-i18n");
    const txt = dict[key];
    if (!txt) return;
    el.textContent = txt;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el => {
    const key = el.getAttribute("data-i18n-placeholder");
    const txt = dict[key];
    if (!txt) return;
    el.placeholder = txt;
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const lang = localStorage.getItem("appLang") || "vi";
  applyLanguage(lang);
});
</script>

<script>
// ===== Video hướng dẫn & sidebar =====
const videosMap = {{ videos|tojson }};
const videoKeys = Object.keys(videosMap || {});
const sel = document.getElementById('exerciseSelect');
const vid = document.getElementById('guideVideo');

window.videosMap = videosMap;
window.EXERCISE_KEYS = videoKeys;
window.currentExerciseIndex = 0;

const btnAddExercise = document.getElementById('btnAddExercise');
if (btnAddExercise && sel) {
  btnAddExercise.addEventListener('click', () => {
    const name = prompt('Nhập tên bài tập mới:');
    if (!name) return;
    const key = name.trim();
    if (!key) return;

    const exists = (window.EXERCISE_KEYS || []).some(k => k.toLowerCase() === key.toLowerCase());
    if (exists) { alert('Bài tập này đã có trong danh sách.'); return; }

    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = key;
    sel.appendChild(opt);

    window.EXERCISE_KEYS.push(key);
    window.videosMap[key] = null;
    sel.value = key;
    window.currentExerciseIndex = window.EXERCISE_KEYS.length - 1;

    if (typeof window.updateVideo === 'function') window.updateVideo(key);
  });
}

window.updateVideo = function(forceKey){
  if (!vid) return;
  let key = forceKey;
  if (!key){
    if (sel && sel.value) key = sel.value;
    else if (videoKeys.length) key = videoKeys[window.currentExerciseIndex] || videoKeys[0];
  }
  if (!key || !videosMap[key]){
    vid.removeAttribute('src');
    vid.load();
    return;
  }
  const idx = videoKeys.indexOf(key);
  window.currentExerciseIndex = idx >= 0 ? idx : 0;
  if (sel && sel.value !== key) sel.value = key;

  const url = videosMap[key];
  if (vid.getAttribute('src') !== url){
    vid.setAttribute('src', url);
    vid.load();
  }
  vid.play().catch(()=>{});
};

if (sel){
  sel.addEventListener('change', () => window.updateVideo(sel.value));
}
window.updateVideo();

document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* ===== Modal chọn bệnh nhân & fill form bên phải ===== */
let PAT_CACHE = null;

function fillPatientOnDashboard(rec){
  const name = rec.name || "";
  const cccd = rec.ID || "";
  const dob = rec.DateOfBirth || "";
  const gender = rec.Gender || "";
  const weight = rec.Weight || "";
  const height = rec.Height || "";
  const code = rec.PatientCode || rec.Patientcode || "";

  document.getElementById('pat_name').value = name;
  document.getElementById('pat_cccd').value = cccd;
  document.getElementById('pat_dob').value = dob;
  document.getElementById('pat_gender').value = gender;
  document.getElementById('pat_weight').value = weight;
  document.getElementById('pat_height').value = height;

  const codeInput = document.getElementById('pat_code');
  if (codeInput) codeInput.value = code;

  try{
    localStorage.setItem("currentPatient", JSON.stringify({ code, name, cccd, dob, gender, weight, height }));
  }catch(e){ console.warn("Không lưu được currentPatient:", e); }
}

function loadCurrentPatientFromLocalStorage(){
  try{
    const raw = localStorage.getItem("currentPatient");
    if (!raw) return;
    const p = JSON.parse(raw);
    if (!p) return;

    document.getElementById('pat_name').value = p.name || "";
    document.getElementById('pat_cccd').value = p.cccd || "";
    document.getElementById('pat_dob').value = p.dob || "";
    document.getElementById('pat_gender').value = p.gender || "";
    document.getElementById('pat_weight').value = p.weight || "";
    document.getElementById('pat_height').value = p.height || "";

    const codeInput = document.getElementById('pat_code');
    if (codeInput) codeInput.value = p.code || "";
  }catch(e){ console.warn("Không đọc được currentPatient từ localStorage:", e); }
}

function renderPatRows(rows){
  const tbody = document.getElementById('pm_body');
  tbody.innerHTML = "";
  rows.forEach((r,i)=>{
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td>${i+1}</td>` +
      `<td>${r.code||""}</td>` +
      `<td>${r.full_name||""}</td>` +
      `<td>${r.national_id||""}</td>` +
      `<td>${r.dob||""}</td>` +
      `<td>${r.sex||""}</td>`;
    tr.addEventListener('dblclick', ()=>{
      const rec = (PAT_CACHE.raw || {})[r.code] || {};
      fillPatientOnDashboard(rec);
      const modal = bootstrap.Modal.getInstance(document.getElementById('patientModal'));
      modal && modal.hide();
    });
    tbody.appendChild(tr);
  });
}

document.getElementById('btnPatientList').addEventListener('click', async (e)=>{
  e.preventDefault();
  const tbody = document.getElementById('pm_body');
  tbody.innerHTML = "<tr><td colspan='6'>Đang tải...</td></tr>";
  try{
    if (!PAT_CACHE){
      const res = await fetch('/api/patients');
      PAT_CACHE = await res.json();
    }
    renderPatRows(PAT_CACHE.rows || []);
  }catch(err){
    tbody.innerHTML = "<tr><td colspan='6'>Lỗi tải dữ liệu</td></tr>";
    console.error(err);
  }
  document.getElementById('pm_search').value = "";
  const modal = new bootstrap.Modal(document.getElementById('patientModal'));
  modal.show();
});

document.getElementById('pm_search').addEventListener('input', (e)=>{
  const kw = e.target.value.toLowerCase();
  const trs = document.querySelectorAll('#pm_body tr');
  trs.forEach(tr=>{
    tr.style.display = tr.innerText.toLowerCase().includes(kw) ? "" : "none";
  });
});
</script>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<script type="module">
import * as THREE from 'https://unpkg.com/three@0.154.0/build/three.module.js';
window.THREE = THREE;
import { GLTFLoader } from 'https://unpkg.com/three@0.154.0/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'https://unpkg.com/three@0.154.0/examples/jsm/controls/OrbitControls.js';

const mount = document.getElementById('threeMount');
const statusEl = document.getElementById('status3D');

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xeaf2ff);

const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
camera.position.set(0, 120, 260);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(mount.clientWidth, mount.clientHeight);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.0;
mount.appendChild(renderer.domElement);
renderer.domElement.style.width = "100%";
renderer.domElement.style.height = "100%";
renderer.domElement.style.display = "block";

scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.3));
const dir = new THREE.DirectionalLight(0xffffff, 1.1);
dir.position.set(2, 4, 2);
scene.add(dir);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.enablePan = false;
controls.enableRotate = false;

const GRID_SIZE = 240;
const grid = new THREE.GridHelper(GRID_SIZE, 24, 0x999999, 0xcccccc);
grid.position.y = 0;
scene.add(grid);

function resizeNow() {
  const w = mount.clientWidth || 1;
  const h = mount.clientHeight || 1;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
new ResizeObserver(resizeNow).observe(mount);
window.addEventListener('resize', resizeNow);
resizeNow();

const legPivot = new THREE.Group();
legPivot.position.set(0, 0, 0);
scene.add(legPivot);
window.legPivot = legPivot;

const loader = new GLTFLoader();
const GLB_URL = "{{ url_for('static', filename='leg_model.glb') }}";

loader.load(
  GLB_URL,
  (gltf) => {
    const model = gltf.scene || gltf.scenes?.[0];
    if (!model) { statusEl.textContent = "⚠️ GLB không có scene."; return; }

    window.SKINS = [];
    model.traverse((o) => {
      if (o.isSkinnedMesh) {
        o.frustumCulled = false;
        o.castShadow = o.receiveShadow = true;
        window.SKINS.push(o);
      } else if (o.isMesh) {
        o.visible = false;
      }
    });

    model.rotation.set(0, 0, 0);
    model.scale.set(1, 1, 1);
    model.updateMatrixWorld(true);

    for (const sm of window.SKINS) {
      sm.normalizeSkinWeights();
      sm.skeleton.pose();
      sm.skeleton.calculateInverses();
      sm.bind(sm.skeleton);
    }

    legPivot.add(model);
    legPivot.rotation.y = Math.PI;

    const box0 = new THREE.Box3().setFromObject(model);
    const size0 = new THREE.Vector3(); box0.getSize(size0);
    const center0 = new THREE.Vector3(); box0.getCenter(center0);
    model.position.sub(center0);
    model.updateMatrixWorld(true);

    const maxDim = Math.max(size0.x, size0.y, size0.z) || 1;
    const TARGET = GRID_SIZE * 0.55;
    const scale = TARGET / maxDim;
    model.scale.setScalar(scale);
    model.updateMatrixWorld(true);

    const box1 = new THREE.Box3().setFromObject(model);
    model.position.y += -box1.min.y;
    model.updateMatrixWorld(true);

    const box2 = new THREE.Box3().setFromObject(model);
    const c2 = box2.getCenter(new THREE.Vector3());
    model.position.x -= c2.x;
    model.position.z -= c2.z;
    model.updateMatrixWorld(true);

    const sphere = new THREE.Sphere();
    new THREE.Box3().setFromObject(model).getBoundingSphere(sphere);
    const sideDist = sphere.radius * 2.2;
    camera.position.set(sideDist, sphere.radius * 0.35, 0);
    camera.lookAt(0, sphere.center.y, 0);
    controls.target.set(0, sphere.center.y, 0);
    controls.update();
    controls.minDistance = sphere.radius * 0.8;
    controls.maxDistance = sphere.radius * 3.0;

    const BONE_REG = new Map();
    for (const sm of window.SKINS) {
      for (const b of sm.skeleton.bones) {
        const key = (b.name || '').toLowerCase();
        if (!key) continue;
        if (!BONE_REG.has(key)) BONE_REG.set(key, []);
        BONE_REG.get(key).push(b);
        if (!b.userData.bindQ) b.userData.bindQ = b.quaternion.clone();
      }
    }

    const NAME_MAP = { hip: 'thighL', knee: 'shinL', ankle: 'footL' };
    function getBones(joint) {
      const key = (NAME_MAP[joint] || '').toLowerCase();
      return BONE_REG.get(key) || [];
    }

    const AXISVEC = { x:new THREE.Vector3(1,0,0), y:new THREE.Vector3(0,1,0), z:new THREE.Vector3(0,0,1) };
    const AXIS = { hip:'x', knee:'x', ankle:'x' };
    const SIGN = { hip:-1, knee: 1, ankle: 1 };
    const OFF  = { hip: 0, knee: 0, ankle:-90 };
    const toRad = d => (Number(d)||0) * Math.PI/180;

    function setJointDeg(joint, deg){
      const bones = getBones(joint);
      if (!bones.length) return;
      const ax = AXISVEC[AXIS[joint]] || AXISVEC.x;
      const qDelta = new THREE.Quaternion().setFromAxisAngle(ax, SIGN[joint]*toRad((OFF[joint]||0) + (Number(deg)||0)));
      for (const b of bones) {
        const q0 = b.userData.bindQ || b.quaternion;
        b.quaternion.copy(q0).multiply(qDelta);
      }
    }

    window.applyLegAngles = (hip, knee, ankle_real) => {
      setJointDeg('hip', hip);
      setJointDeg('knee', knee);
      setJointDeg('ankle', ankle_real);
    };

    window.legReady = true;
    if (window._pendingAngles) {
      const a = window._pendingAngles;
      window._pendingAngles = null;
      window.applyLegAngles(a.hip, a.knee, a.ankle);
    }

    document.getElementById('btnResetPose3D')?.addEventListener('click', () => {
      for (const arr of BONE_REG.values()) for (const b of arr) if (b.userData.bindQ) b.quaternion.copy(b.userData.bindQ);
    });

    const bbox = new THREE.Box3().setFromObject(model);
    const size = bbox.getSize(new THREE.Vector3());
    const rad = size.length() * 0.5 || 1;
    camera.near = Math.max(0.1, rad * 0.01);
    camera.far  = rad * 20;
    camera.updateProjectionMatrix();
  },
  (progress) => {
    const percent = (progress.loaded / (progress.total || 1)) * 100;
    statusEl.textContent = `Đang tải mô hình: ${percent.toFixed(0)}%`;
  },
  (err) => { console.error("❌ Lỗi load GLB:", err); }
);

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

window.pushAngles = (hip, knee, ankle) => {
  if (window.legReady && typeof window.applyLegAngles === "function") {
    window.applyLegAngles(hip, knee, ankle);
  } else {
    window._pendingAngles = { hip, knee, ankle };
  }
};
</script>

<!-- Socket & Start/Stop + VAS -->
<script id="imu-handlers">
const btnSave = document.getElementById("btnSave");
const btnStart = document.getElementById("btnStart");
const btnStop  = document.getElementById("btnStop");
const exerciseSelect = document.getElementById("exerciseSelect");
const resultPanel = document.getElementById("exercise-result-panel");
const summaryPanel = document.getElementById("all-exercise-summary");
const btnNextEx = document.getElementById("btn-next-ex");

if (btnStop) btnStop.disabled = true;

// ===== GIẢ LẬP NHỊP TIM – chỉ chạy khi đang đo =====
let heartSimTimer = null;
let heartVal = 75;
let heartDir = 1;

function startHeartSim(){
  const el = document.getElementById("heartRate");
  if (!el) return;
  if (heartSimTimer) return;

  const MIN = 70;
  const MAX = 95;

  function step(){
    if (!isMeasuring){
      heartSimTimer = null;
      return;
    }
    heartVal += heartDir * (Math.random() * 1.5 + 0.5);
    if (heartVal >= MAX){ heartVal = MAX; heartDir = -1; }
    if (heartVal <= MIN){ heartVal = MIN; heartDir = 1; }
    el.value = heartVal.toFixed(0);
    heartSimTimer = setTimeout(step, Math.random()*400 + 300);
  }

  heartVal = 75;
  heartDir = 1;
  step();
}

function stopHeartSim(){
  if (heartSimTimer){
    clearTimeout(heartSimTimer);
    heartSimTimer = null;
  }
}

// ====== HÀM CHẤM ĐIỂM FMA (0–2) theo ROM Knee ======
function fmaScore(rom){
  rom = Number(rom) || 0;
  if (rom >= 90) return 2;
  if (rom >= 40 && rom <= 50) return 1;
  if (rom < 10) return 0;
  return 1;
}

// ====== STATE ĐO TỪNG BÀI ======
const EXERCISE_ORDER = (window.EXERCISE_KEYS && window.EXERCISE_KEYS.length)
  ? window.EXERCISE_KEYS
  : ["ankle flexion","knee flexion","hip flexion"];

let isMeasuring = false;
let currentSamples = []; // {hip,knee,ankle}

function getCurrentExerciseName(){
  return exerciseSelect ? (exerciseSelect.value || "exercise") : "exercise";
}

function getExerciseRegion(){
  const name = getCurrentExerciseName().toLowerCase();
  if (name.includes("hip")) return "hip";
  if (name.includes("knee")) return "knee";
  if (name.includes("ankle")) return "ankle";
  return "hip";
}

/* =========================
   EMG chỉ cho KNEE
   - sensor_id = 5
   - ẩn card khi không phải knee
========================= */
const emgCard = document.getElementById("emgCard");

function isEmgEnabled(){
  return true; 
}


/*  FIX LỖI: trước đây gọi syncEmgVisibility nhưng chưa định nghĩa */
function syncEmgVisibility(){
  const card = document.getElementById("emgCard");
  if (!card) return;
  card.classList.remove("emg-hidden"); // luôn hiện
}


/* đổi bài tập -> reset + sync */
exerciseSelect?.addEventListener("change", ()=>{
  resetEmgBuffer();
  syncEmgVisibility();
  if (isEmgEnabled()){
    ensureEmgChart();
    emgUpdateChart();
  }
});

/* ================= EMG WAVEFORM (Chart.js) ================= */
const emgCanvas = document.getElementById("emgChart");
let emgChartObj = null;
let emgBuf = [];         // [{t:ms, v:number}]
let emgRmsWindow = [];   // rms window

function resetEmgBuffer(){
  emgBuf = [];
  emgRmsWindow = [];
  const rmsEl = document.getElementById("emgRmsTxt");
  if (rmsEl) rmsEl.textContent = "--";
  if (emgChartObj){
    emgChartObj.data.datasets[0].data = [];
    emgChartObj.update("none");
  }
}

function pickEmgValue(msg){
  // Backend có thể gửi sender_id hoặc emg_id hoặc sensor_id
  const sidRaw = (msg && (msg.sender_id ?? msg.emg_id ?? msg.sensor_id)) ;
  const sid = sidRaw != null ? Number(sidRaw) : null;

  // CHỈ nhận sensor 5
  if (sid !== 5) return null;

  // emg có thể là số hoặc object {v:...}
  let v = null;
  if (msg && msg.emg != null){
    if (typeof msg.emg === "number") v = msg.emg;
    else if (typeof msg.emg === "string") v = Number(msg.emg);
    else if (typeof msg.emg === "object" && msg.emg.v != null) v = Number(msg.emg.v);
  }

  return (v != null && !Number.isNaN(v)) ? v : null;
}


function ensureEmgChart(){
  if (!emgCanvas || emgChartObj) return;

  const ctx = emgCanvas.getContext("2d");
  emgChartObj = new Chart(ctx, {
    type: "line",
    data: { datasets: [{ label: "EMG (µV)", data: [], pointRadius: 0, borderWidth: 2, tension: 0 }] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      normalized: true,
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      elements: { line: { capBezierPoints: false } },
      scales: {
        x: {
          type: "linear",
          ticks: { maxTicksLimit: 6, color: "#2b3a4a", font: { weight: "700" } },
          grid: { color: "rgba(8,58,106,.10)", lineWidth: 1 },
          border: { color: "rgba(8,58,106,.18)" }
        },
        y: {
          ticks: { maxTicksLimit: 6, color: "#2b3a4a", font: { weight: "700" } },
          grid: { color: "rgba(8,58,106,.10)", lineWidth: 1 },
          border: { color: "rgba(8,58,106,.18)" }
        }
      }
    }
  });
}

function emgTrimToWindow(winSec){
  const now = performance.now();
  const cutoff = now - winSec * 1000;
  while (emgBuf.length && emgBuf[0].t < cutoff) emgBuf.shift();
}
function emgAutoScale(){
  // lấy max |v| trong window hiện tại
  let m = 0;
  for (const p of emgBuf){
    const a = Math.abs(Number(p.v) || 0);
    if (a > m) m = a;
  }
  // tránh scale = 0
  if (m < 5) m = 5;

  // nới biên 20% cho dễ nhìn
  return Math.ceil(m * 1.2);
}

function emgUpdateChart(){
  if (!emgChartObj) return;

  const winSec = Number(document.getElementById("emgWin")?.value || 5);

  // scale người dùng nhập = mức tối thiểu
  let scaleMin = Number(document.getElementById("emgScale")?.value || 1000);

  const winTxt = document.getElementById("emgWinTxt");
  const scTxt  = document.getElementById("emgScaleTxt");
  if (winTxt) winTxt.textContent = String(winSec);

  // cắt buffer theo window
  emgTrimToWindow(winSec);

  // chuẩn hoá trục thời gian
  const now  = performance.now();
  const base = now - winSec * 1000;

  const pts = emgBuf.map(p => ({
    x: (p.t - base) / 1000.0,
    y: Number(p.v) || 0
  }));

  emgChartObj.data.datasets[0].data = pts;

  // ===== AUTO SCALE =====
  // lấy max |y| trong window hiện tại
  let maxAbs = 0;
  for (const pt of pts){
    const a = Math.abs(pt.y);
    if (a > maxAbs) maxAbs = a;
  }

  // nới biên 20% cho dễ nhìn, chống =0
  let autoScale = Math.ceil(Math.max(5, maxAbs * 1.2));

  // scale cuối cùng: không nhỏ hơn scaleMin
  const scale = Math.max(scaleMin, autoScale);

  if (scTxt) scTxt.textContent = "±" + String(scale);

  // set trục
  emgChartObj.options.scales.y.min = -scale;
  emgChartObj.options.scales.y.max = +scale;
  emgChartObj.options.scales.x.min = 0;
  emgChartObj.options.scales.x.max = winSec;

  emgChartObj.update("none");
}


function emgPushValue(v){
  const t = performance.now();
  emgBuf.push({ t, v });

  emgRmsWindow.push(v);
  if (emgRmsWindow.length > 200) emgRmsWindow.shift();

  let rms = 0;
  if (emgRmsWindow.length){
    let s2 = 0;
    for (const x of emgRmsWindow) s2 += x*x;
    rms = Math.sqrt(s2 / emgRmsWindow.length);
  }
  const rmsEl = document.getElementById("emgRmsTxt");
  if (rmsEl) rmsEl.textContent = rms ? rms.toFixed(1) : "--";
}

/* update chart định kỳ (chỉ khi knee) */
setInterval(() => {
  if (!isEmgEnabled()) return;
  ensureEmgChart();
  emgUpdateChart();
}, 80);

["emgWin","emgScale"].forEach(id=>{
  document.getElementById(id)?.addEventListener("change", ()=>{
    if (!isEmgEnabled()) return;
    ensureEmgChart();
    emgUpdateChart();
  });
});

/* ========== VAS STATE & HÀM ========== */
const VAS_TEXT_VI = [
  "Không đau",
  "Đau rất nhẹ, thỉnh thoảng mới cảm thấy.",
  "Đau rất nhẹ, hơi khó chịu nhưng vẫn sinh hoạt bình thường.",
  "Đau nhẹ, cảm nhận rõ nhưng vẫn chịu được.",
  "Đau nhẹ đến trung bình, bắt đầu thấy phiền khi vận động.",
  "Đau trung bình, ảnh hưởng một phần sinh hoạt.",
  "Đau trung bình đến nhiều, cần nghỉ ngơi xen kẽ khi hoạt động.",
  "Đau nhiều, khó tiếp tục hoạt động bình thường.",
  "Đau rất nhiều, rất khó chịu, phải giảm hầu hết hoạt động.",
  "Đau gần như không chịu nổi, cần hỗ trợ/thuốc giảm đau.",
  "Mức đau tệ nhất bạn có thể tưởng tượng hoặc từng trải qua."
];

let currentVAS = 0;

function selectVASTick(val) {
  val = parseInt(val);
  currentVAS = val;

  document.querySelectorAll(".vas-tick").forEach(t => t.classList.remove("active"));
  const tick = document.querySelector(`.vas-tick[data-value="${val}"]`);
  if (tick) tick.classList.add("active");

  const desc = VAS_TEXT_VI[val] || "";
  const label = document.getElementById("vasSelected");
  if (label) label.innerText = `${val} – ${desc}`;
}

function resetVASDefault() { selectVASTick(0); }

function saveVAS(region, phase, val){
  const patCode = (document.getElementById("pat_code")?.value || "").trim();
  const payload = {
    exercise_region: region,
    phase: phase,
    vas: val,
    patient_code: patCode,
    exercise_name: getCurrentExerciseName()
  };
  fetch("/save_vas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  })
  .then(r => r.json())
  .then(res => console.log("[VAS] saved:", res))
  .catch(err => console.warn("[VAS] error saving (có thể chưa tạo route /save_vas):", err));
}

function openVASModal(region, phase, onConfirm){
  const titleEl = document.getElementById("vasModalTitle");
  const subEl = document.getElementById("vasModalSubtitle");

  let title = "Đánh giá mức độ đau (VAS)";
  let sub = "Vui lòng chọn mức độ đau từ 0 (không đau) đến 10 (đau tệ nhất).";

  if (region === "hip") {
    title = (phase === "before") ? "Mức độ đau vùng hông TRƯỚC khi tập" : "Mức độ đau vùng hông SAU khi tập";
  } else if (region === "knee") {
    title = (phase === "before") ? "Mức độ đau vùng quanh gối (cơ đùi trước – sau) TRƯỚC khi tập" : "Mức độ đau vùng quanh gối (cơ đùi trước – sau) SAU khi tập";
  } else if (region === "ankle") {
    title = (phase === "before") ? "Mức độ đau vùng cổ chân TRƯỚC khi tập" : "Mức độ đau vùng cổ chân SAU khi tập";
  }

  if (titleEl) titleEl.innerText = title;
  if (subEl) subEl.innerText = sub;

  resetVASDefault();

  const modalEl = document.getElementById("vasModal");
  if (!modalEl) return;
  const modal = new bootstrap.Modal(modalEl);

  const btnConfirm = document.getElementById("vasConfirmBtn");
  if (!btnConfirm) return;

  btnConfirm.onclick = () => {
    saveVAS(region, phase, currentVAS);
    modal.hide();
    if (typeof onConfirm === "function") onConfirm();
  };

  modal.show();
}

/* ========== NÚT "LƯU KẾT QUẢ" ========== */
if (btnSave) btnSave.addEventListener("click", async () => {
  const name = document.getElementById('pat_name').value.trim();
  const cccd = document.getElementById('pat_cccd').value.trim();
  const dob = document.getElementById('pat_dob').value.trim();
  const gender = document.getElementById('pat_gender').value.trim();
  const weight = document.getElementById('pat_weight').value.trim();
  const height = document.getElementById('pat_height').value.trim();
  const codeEl = document.getElementById('pat_code');
  let patient_code = codeEl ? (codeEl.value || "").trim() : "";
  const measureDate = document.getElementById('measure_date')?.value || "";

  if (!name){
    alert("Vui lòng nhập HỌ VÀ TÊN bệnh nhân trước khi lưu.");
    return;
  }

  const patientPayload = { patient_code, name, national_id: cccd, dob, gender, weight, height };
  try {
    const res = await fetch("/api/patients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patientPayload)
    });
    const j = await res.json();
    if (!j.ok) { alert(j.msg || "Lưu thông tin bệnh nhân thất bại."); return; }
    if (codeEl && j.patient_code) { codeEl.value = j.patient_code; patient_code = j.patient_code; }

    try{
      localStorage.setItem("currentPatient", JSON.stringify({ code: patient_code, name, cccd, dob, gender, weight, height }));
    }catch(e){ console.warn("Không lưu currentPatient sau khi Save:", e); }
  } catch (e) {
    console.error(e);
    alert("Có lỗi khi gửi dữ liệu bệnh nhân lên server.");
    return;
  }

  let exerciseScores = {};
  try { exerciseScores = JSON.parse(localStorage.getItem("exerciseScores") || "{}"); }
  catch (e) { exerciseScores = {}; }

  const recordPayload = {
    patient_code,
    measure_date: measureDate,
    patient_info: { name, cccd, dob, gender, weight, height },
    exercise_scores: exerciseScores
  };

  try {
    const res2 = await fetch("/api/save_record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(recordPayload)
    });
    const j2 = await res2.json();
    if (!j2.ok) { alert(j2.msg || "Lưu bệnh án không thành công."); return; }
    alert("✅ Đã lưu đầy đủ bệnh án (thông tin + điểm bài tập + VAS).");
  } catch (e) {
    console.error(e);
    alert("Có lỗi khi gửi bệnh án lên server.");
  }
});

/* ========== SOCKET IO – cập nhật EMG + góc & thu mẫu ========== */
window.socket = window.socket || io({
  transports: ['websocket'],
  upgrade: false,
  reconnection: true,
  reconnectionAttempts: 10,
  reconnectionDelay: 500
});

const socket = window.socket;
socket.on('connect', () => console.log('[SOCKET] connected:', socket.id));
socket.on('connect_error', (e) => console.error('[SOCKET] connect_error:', e));
socket.on('disconnect', (r) => console.warn('[SOCKET] disconnected:', r));

socket.on("imu_data", (msg) => {
  // ===== EMG waveform: CHỈ VẼ KHI CHỌN KNEE FLEXION =====
  if (isEmgEnabled()) {
    const emgVal = pickEmgValue(msg); // pickEmgValue đọc msg.emg và lọc sensor 5
    if (emgVal != null) emgPushValue(emgVal);
  }

  // Badge góc dưới 3D
  if (msg.hip   != null) document.getElementById('liveHip').textContent   = Number(msg.hip).toFixed(1);
  if (msg.knee  != null) document.getElementById('liveKnee').textContent  = Number(msg.knee).toFixed(1);
  if (msg.ankle != null) document.getElementById('liveAnkle').textContent = Number(msg.ankle).toFixed(1);

  // Thu mẫu ROM
  if (isMeasuring){
    const hip = Number(msg.hip ?? 0);
    const knee = Number(msg.knee ?? 0);
    const ankle = Number(msg.ankle ?? 0);
    currentSamples.push({hip,knee,ankle});
  }

  // 3D
  const hip = msg.hip ?? 0;
  const knee = msg.knee ?? 0;
  const ankle = msg.ankle ?? 0;
  if (typeof window.pushAngles === "function") {
    window.pushAngles(hip, knee, ankle);
  } else {
    window._pendingAngles = { hip, knee, ankle };
  }
});

/* ========== CORE: THỰC SỰ START/STOP ĐO ========== */
async function reallyStartMeasurement(){
  if (isMeasuring) return;

  try{
    const curName = getCurrentExerciseName();
    const firstName = EXERCISE_ORDER[0];
    if (curName === firstName) localStorage.removeItem("exerciseScores");
  }catch(e){}

  const r = await fetch("/session/start", { method: "POST" });
  const j = await r.json();
  console.log("[START RESPONSE]", j);

  if (!j.ok) { alert(j.msg || "Không start được phiên đo"); return; }

  isMeasuring = true;
  currentSamples = [];
  startHeartSim();

  btnStart.disabled = true;
  btnStart.textContent = "Đang đo...";
  btnStop.disabled = false;
  btnStop.textContent = "Kết thúc đo";

  resultPanel.style.display = "none";
  summaryPanel.style.display = "none";

  /* ✅ start đo -> reset EMG buffer cho sạch */
  resetEmgBuffer();
  syncEmgVisibility();
  if (isEmgEnabled()){
    ensureEmgChart();
    emgUpdateChart();
  }
}

async function reallyStopMeasurement(){
  if (!isMeasuring) return null;

  const r = await fetch("/session/stop", { method: "POST" });
  try { await r.json(); } catch(e){}

  isMeasuring = false;
  stopHeartSim();

  btnStart.disabled = false;
  btnStop.disabled = true;
  btnStart.textContent = "Bắt đầu đo";

  let romHip = 0, romKnee = 0, romAnkle = 0, score = 0;
  let maxKnee = 0, minKnee = 0;

  if (currentSamples.length){
    const hips = currentSamples.map(s => s.hip);
    const knees = currentSamples.map(s => s.knee);
    const ankles = currentSamples.map(s => s.ankle);

    const maxHip = Math.max(...hips);
    const minHip = Math.min(...hips);
    maxKnee = Math.max(...knees);
    minKnee = Math.min(...knees);
    const maxAnkle = Math.max(...ankles);
    const minAnkle = Math.min(...ankles);

    romHip = maxHip - minHip;
    romKnee = maxKnee - minKnee;
    romAnkle = maxAnkle - minAnkle;

    score = fmaScore(romKnee);
  }

  const exName = getCurrentExerciseName();
  const result = { name: exName, romHip, romKnee, romAnkle, score, maxKnee, minKnee };

  let store = {};
  try { store = JSON.parse(localStorage.getItem("exerciseScores") || "{}"); }
  catch(e){ store = {}; }

  store[exName] = result;
  localStorage.setItem("exerciseScores", JSON.stringify(store));

  const pat = (document.getElementById("pat_code")?.value || "").trim();
  let url = "/charts?exercise=" + encodeURIComponent(exName);
  if (pat) url += "&patient_code=" + encodeURIComponent(pat);
  return url;
}

/* ========== NÚT BẮT ĐẦU / KẾT THÚC ĐO: THÊM VAS TRƯỚC & SAU ========== */
if (btnStart) btnStart.addEventListener("click", () => {
  const region = getExerciseRegion();
  openVASModal(region, "before", () => { reallyStartMeasurement(); });
});

if (btnStop) btnStop.addEventListener("click", async () => {
  const url = await reallyStopMeasurement();
  if (!url) return;
  const region = getExerciseRegion();
  openVASModal(region, "after", () => { window.location.href = url; });
});

// Khởi tạo VAS + load bệnh nhân + sync EMG
document.addEventListener("DOMContentLoaded", () => {
  resetVASDefault();
  loadCurrentPatientFromLocalStorage();
  syncEmgVisibility();
  if (isEmgEnabled()){
    ensureEmgChart();
    emgUpdateChart();
  }
});
</script>

</body></html>"""




SETTINGS_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title data-i18n="settings.page_title">Cài đặt – IMU Dashboard</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">

<style>
:root{ --blue:#1669c9; --soft:#f3f7ff; --sbw:260px; }
body{ background:#fafbfe; font-size:15px; }
.layout{ display:flex; gap:16px; position:relative; }

.sidebar{ background:var(--blue); color:#fff; border-top-right-radius:16px; border-bottom-right-radius:16px; padding:16px; width:var(--sbw); min-height:100vh; box-sizing:border-box; }
.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); }
.main-col{ flex:1 1 auto; min-width:0; }

.menu-btn{
  width:100%; display:block; background:#1973d4; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600;
  text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; color:#fff; }
.menu-btn.active{ background:#0b4fa0; }

.panel{
  background:#fff; border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,.06);
  padding:16px 20px;
}
.form-label{ font-weight:600; color:#244e78 }
</style>
</head>

<body>
<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2" style="border:2px solid #d8e6ff;border-radius:10px;background:#fff;">☰</button>

    <span class="navbar-brand mb-0">
      <span data-i18n="nav.hello">Xin chào,</span> {{username}}
    </span>

    <div class="ms-auto d-flex align-items-center gap-2">
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">

    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>

        <a class="menu-btn" href="/"                data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn" href="/calibration"     data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage" data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/patients"        data-i18n="menu.review">Xem lại</a>
        <a class="menu-btn" href="/records"         data-i18n="menu.records">Bệnh án</a>
        <a class="menu-btn" href="/charts"          data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn active" href="/settings" data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="panel mb-3">
        <h5 class="mb-3" data-i18n="settings.title">Cài đặt hệ thống</h5>

        <div class="row g-3">
          <div class="col-md-4">
            <label class="form-label" data-i18n="settings.language">Ngôn ngữ hiển thị</label>
            <select id="languageSelect" class="form-select">
              <option value="vi" data-i18n="settings.lang_vi">Tiếng Việt</option>
              <option value="en" data-i18n="settings.lang_en">English</option>
            </select>
          </div>
        </div>

        <div class="mt-4 d-flex gap-2">
          <button id="btnSaveSettings" class="btn btn-primary" data-i18n="settings.btn_save">Lưu cài đặt</button>
          <span id="settingsStatus" class="text-success small" style="display:none;" data-i18n="settings.saved">Đã lưu!</span>
        </div>
      </div>

      <!-- Account block -->
      <div class="panel">
        <h6 class="mb-2" data-i18n="settings.account_title">Tài khoản</h6>
        <p class="small text-muted mb-3">
          <span data-i18n="settings.logout_text">Đăng xuất khỏi tài khoản</span>
          <strong>{{username}}</strong>
          <span data-i18n="settings.logout_suffix">hiện tại.</span>
        </p>
        <a href="/logout" class="btn btn-outline-danger" data-i18n="settings.btn_logout">Đăng xuất</a>
      </div>

    </main>
  </div>
</div>

<script>
// ===================== I18N TABLE =====================
const I18N = {
  vi: {
    "settings.page_title": "Cài đặt – IMU Dashboard",
    "nav.hello": "Xin chào,",
    "menu.title": "MENU",
    "menu.home": "Trang chủ",
    "menu.calib": "Hiệu chuẩn",
    "menu.patinfo": "Thông tin bệnh nhân",
    "menu.review": "Xem lại",
    "menu.records": "Bệnh án",
    "menu.charts": "Biểu đồ",
    "menu.settings": "Cài đặt",

    "settings.title": "Cài đặt hệ thống",
    "settings.language": "Ngôn ngữ hiển thị",
    "settings.lang_vi": "Tiếng Việt",
    "settings.lang_en": "English",
    "settings.btn_save": "Lưu cài đặt",
    "settings.saved": "Đã lưu!",
    "settings.account_title": "Tài khoản",
    "settings.logout_text": "Đăng xuất khỏi tài khoản",
    "settings.logout_suffix": "hiện tại.",
    "settings.btn_logout": "Đăng xuất"
  },

  en: {
    "settings.page_title": "Settings – IMU Dashboard",
    "nav.hello": "Hello,",
    "menu.title": "MENU",
    "menu.home": "Home",
    "menu.calib": "Calibration",
    "menu.patinfo": "Patient information",
    "menu.review": "Review",
    "menu.records": "Records",
    "menu.charts": "Charts",
    "menu.settings": "Settings",

    "settings.title": "System settings",
    "settings.language": "Display language",
    "settings.lang_vi": "Vietnamese",
    "settings.lang_en": "English",
    "settings.btn_save": "Save settings",
    "settings.saved": "Saved!",
    "settings.account_title": "Account",
    "settings.logout_text": "Log out from account",
    "settings.logout_suffix": "now.",
    "settings.btn_logout": "Log out"
  }
};

// ===================== APPLY LANGUAGE =====================
function applyLanguage(lang){
  const dict = I18N[lang] || I18N.vi;

  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k = el.getAttribute("data-i18n");
    if (dict[k]) el.textContent = dict[k];
  });

  // Placeholder support (nếu cần)
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el=>{
    const k = el.getAttribute("data-i18n-placeholder");
    if (dict[k]) el.placeholder = dict[k];
  });

  // Update title
  const titleEl = document.querySelector("title[data-i18n]");
  if (titleEl){
    const k = titleEl.getAttribute("data-i18n");
    if (dict[k]) titleEl.textContent = dict[k];
  }
}

// ===================== LOAD & SAVE SETTINGS =====================
function loadSettings(){
  let lang = localStorage.getItem("appLang") || "vi";
  document.getElementById("languageSelect").value = lang;
  applyLanguage(lang);
}

function saveSettings(){
  const lang = document.getElementById("languageSelect").value;
  localStorage.setItem("appLang", lang);
  applyLanguage(lang);

  const st = document.getElementById("settingsStatus");
  st.style.display = "inline";
  setTimeout(()=> st.style.display = "none", 1500);
}

// ===================== EVENT BINDING =====================
document.getElementById("btnSaveSettings").onclick = saveSettings;
document.getElementById("btnToggleSB").onclick = () => {
  document.body.classList.toggle("sb-collapsed");
};

document.addEventListener("DOMContentLoaded", loadSettings);

</script>
</body></html>
"""

CHARTS_HTML = r"""
<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biểu đồ góc khớp</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root { --blue:#1669c9; --sbw:260px; }

body{ background:#e8f3ff; margin:0; font-size:15px; }
.layout{ display:flex; gap:16px; position:relative; }

.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); transition:all .28s ease; }
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px; border-bottom-right-radius:16px;
  padding:16px; min-height:100vh;
}
.main-col{ flex:1 1 auto; min-width:0; }

body.sb-collapsed .sidebar-col{ flex-basis:0 !important; max-width:0 !important; }
body.sb-collapsed .sidebar{ padding:0 !important; }
body.sb-collapsed .sidebar *{ display:none; }

#btnToggleSB{
  border:2px solid #d8e6ff; background:#fff;
  border-radius:10px; padding:6px 10px; font-weight:700;
}
#btnToggleSB:hover{ background:#eef6ff; }

.menu-btn{
  width:100%; display:block; background:#1d74d8; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px;
  font-weight:600; text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; }
.menu-btn.active{ background:#0f5bb0; }

.panel{
  background:#fff; border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,0.10);
  padding:16px; margin-bottom:16px;
}
.chart-box{ height:260px; }

/* Khối đánh giá */
.eval-panel{
  background:#ffffff; border-radius:18px;
  box-shadow:0 10px 24px rgba(15,23,42,.16);
  padding:18px 18px 14px 18px;
}
.eval-header{ font-weight:800; color:#0b3769; font-size:1.1rem; }
.eval-subtitle{ font-size:.9rem; color:#64748b; }
.eval-item{ font-size:.95rem; }
.eval-item + .eval-item{
  border-top:1px dashed #e2e8f0; margin-top:10px; padding-top:10px;
}
.eval-badge{ font-size:.8rem; padding:4px 8px; border-radius:999px; }
#totalScore{ font-size:.95rem; padding:6px 10px; border-radius:999px; }

.strength-label{ font-weight:700; font-size:1rem; color:#0b3769; }
.strength-desc{
  font-size:.9rem; color:#0b3769; font-weight:500;
  background:#e8f5ff; border-radius:10px;
}

/* Tổng điểm */
.total-summary{
  margin-top:10px; text-align:center; font-weight:800;
  font-size:1.05rem; color:#0b3769;
}
.total-summary span{
  display:inline-block; margin-left:6px; padding:4px 14px;
  border-radius:999px; background:#1d4ed8; color:#fff; font-size:1rem;
}

/* Mini VAS */
.vas-mini-value{ font-weight:700; font-size:1rem; }
.vas-mini-label{ font-size:.85rem; color:#64748b; }
</style>
</head>

<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-3">
      <img src="/static/unnamed.png" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">

    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>
        <a class="menu-btn" href="/"                 data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn" href="/calibration"      data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage"  data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/records"          data-i18n="menu.record">Bệnh án</a>
        <a class="menu-btn active" href="/charts"    data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings"         data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <main class="main-col">
      <div class="row g-3">

        <div class="col-lg-9">
          <div class="panel">
            <div class="d-flex justify-content-between align-items-center">

              <div>
                <h5 class="mb-1" data-i18n="charts.title">Biểu đồ góc khớp theo thời gian</h5>
                <div class="text-muted small" data-i18n="charts.subtitle">Phiên đo gần nhất.</div>

                {% if exercise_name %}
                <div class="text-muted small">
                  <span data-i18n="charts.exercise">Bài tập:</span>
                  <strong>{{ exercise_name }}</strong>
                </div>
                {% endif %}

                {% if patient_code %}
                <div class="text-muted small">
                  <span data-i18n="charts.patient_code">Mã bệnh nhân:</span>
                  <strong>{{ patient_code }}</strong>
                </div>
                {% endif %}
              </div>

              <div class="d-flex gap-2">
                <a class="btn btn-outline-success btn-sm"
                   href="/session/export_csv{% if patient_code %}?patient_code={{ patient_code }}{% endif %}"
                   target="_blank"
                   data-i18n="charts.save_csv">Lưu CSV</a>

                <a class="btn btn-outline-primary btn-sm" href="/charts_emg" data-i18n="charts.emg">EMG</a>

                <button id="btnNextEx" class="btn btn-primary btn-sm" data-i18n="charts.next_ex">
                  Bài tập tiếp theo
                </button>
              </div>

            </div>
          </div>

          <div class="panel"><h6 data-i18n="charts.hip">Hip (độ)</h6><div class="chart-box"><canvas id="hipChart"></canvas></div></div>
          <div class="panel"><h6 data-i18n="charts.knee">Knee (độ)</h6><div class="chart-box"><canvas id="kneeChart"></canvas></div></div>
          <div class="panel"><h6 data-i18n="charts.ankle">Ankle (độ)</h6><div class="chart-box"><canvas id="ankleChart"></canvas></div></div>

          <!-- ✅ EMG CHART (raw/rms/env) ngay trong /charts -->
          <div class="panel">
            <h6 class="mb-2">EMG (raw / RMS / envelope) – 6s cuối</h6>
            <div class="chart-box"><canvas id="emgChart"></canvas></div>
            <div class="small text-muted mt-2">
              Nếu đường EMG không hiện: kiểm tra route /charts đã truyền emg/emg_rms/emg_env và dữ liệu có cùng chiều với t_ms.
            </div>
          </div>
        </div>

        <div class="col-lg-3">

          <!-- 🟦 VAS PANEL -->
          <div class="panel mb-3">
            <div class="eval-header mb-1" data-i18n="charts.vas_title">Đau chủ quan (VAS)</div>
            <div class="eval-subtitle mb-2 small" data-i18n="charts.vas_sub">
              Mức đau trước và sau bài tập hiện tại (0–10).
            </div>

            {% if vas_before is not none or vas_after is not none %}
              <table class="table table-sm mb-2">
                <tbody>
                  <tr>
                    <th scope="row" style="width:45%;" data-i18n="charts.vas_before">Trước khi tập</th>
                    <td class="text-end">
                      {% if vas_before is not none %}
                        <span class="vas-mini-value">{{ '%.1f'|format(vas_before) }}</span>
                        <span class="vas-mini-label">/ 10</span>
                      {% else %}
                        <span class="text-muted small" data-i18n="charts.vas_none">Chưa ghi nhận</span>
                      {% endif %}
                    </td>
                  </tr>
                  <tr>
                    <th scope="row" data-i18n="charts.vas_after">Sau khi tập</th>
                    <td class="text-end">
                      {% if vas_after is not none %}
                        <span class="vas-mini-value">{{ '%.1f'|format(vas_after) }}</span>
                        <span class="vas-mini-label">/ 10</span>
                      {% else %}
                        <span class="text-muted small" data-i18n="charts.vas_none">Chưa ghi nhận</span>
                      {% endif %}
                    </td>
                  </tr>
                </tbody>
              </table>

              {% if vas_before is not none and vas_after is not none %}
                {% set diff = vas_after - vas_before %}
                <div class="small mt-1">
                  {% if diff > 0 %}
                    <span class="badge bg-danger me-1" data-i18n="charts.vas_more">Đau tăng</span>
                    <span class="text-muted">
                      <span data-i18n="charts.vas_more_desc_prefix">Tăng khoảng</span>
                      <strong>{{ '%.1f'|format(diff) }}</strong>
                      <span data-i18n="charts.vas_more_desc_suffix">điểm sau bài tập.</span>
                    </span>
                  {% elif diff < 0 %}
                    <span class="badge bg-success me-1" data-i18n="charts.vas_less">Đau giảm</span>
                    <span class="text-muted">
                      <span data-i18n="charts.vas_less_desc_prefix">Giảm khoảng</span>
                      <strong>{{ '%.1f'|format(-diff) }}</strong>
                      <span data-i18n="charts.vas_less_desc_suffix">điểm sau bài tập.</span>
                    </span>
                  {% else %}
                    <span class="badge bg-secondary me-1" data-i18n="charts.vas_same">Không đổi</span>
                    <span class="text-muted" data-i18n="charts.vas_same_desc">Mức đau không thay đổi sau bài tập.</span>
                  {% endif %}
                </div>
              {% else %}
                <div class="small text-muted mt-1" data-i18n="charts.vas_not_enough">
                  Chưa đủ dữ liệu VAS trước/sau cho bài này.
                </div>
              {% endif %}
            {% else %}
              <div class="small text-muted" data-i18n="charts.vas_no_data">
                Chưa ghi nhận VAS cho bài tập hiện tại.
              </div>
            {% endif %}
          </div>

          <!-- FMA -->
          <div class="eval-panel mb-3">
            <div class="eval-header mb-1" data-i18n="charts.fma_title">Đánh giá FMA</div>

            <div id="evalContent">
              <div class="d-flex align-items-center justify-content-center py-4">
                <div class="spinner-border text-primary me-2"></div>
                <span class="small text-muted" data-i18n="charts.loading">Đang xử lý...</span>
              </div>
            </div>

            <hr class="my-2">

            <div id="totalBox" class="small mb-2">
              <span class="me-1 fw-semibold" data-i18n="charts.current_score">Điểm bài hiện tại:</span>
              <span id="totalScore" class="badge bg-primary ms-1">0 / 2</span>
            </div>

            <hr class="my-2">
            <div class="small fw-bold mb-1" data-i18n="charts.all_ex_summary">Tổng kết các bài đã đo</div>
            <div id="allExercisesSummary" class="small"></div>
          </div>

          <!-- Bảng EMG (demo) -->
          <div class="panel">
            <div class="eval-header mb-1" data-i18n="charts.emg_title">Tín hiệu điện cơ EMG</div>
            <table class="table table-sm mb-0">
              <tbody>
                <tr>
                  <th scope="row" data-i18n="charts.emg_thigh">Cơ đùi</th>
                  <td class="text-end">
                    <span style="
                        background:#dcfce7; color:#166534;
                        padding:4px 10px; border-radius:8px;
                        font-weight:600; font-size:0.85rem;
                    " data-i18n="charts.emg_good">Khỏe</span>
                  </td>
                </tr>
                <tr>
                  <th scope="row" data-i18n="charts.emg_shank">Cơ cẳng chân</th>
                  <td class="text-end text-muted">—</td>
                </tr>
              </tbody>
            </table>
          </div>

        </div>

      </div>
    </main>

  </div>
</div>

<script>
// ======= I18N (giữ nguyên như bạn) =======
const I18N = {
  vi: {
    "menu.title":"MENU","menu.home":"Trang chủ","menu.calib":"Hiệu chuẩn","menu.patinfo":"Thông tin bệnh nhân",
    "menu.record":"Bệnh án","menu.charts":"Biểu đồ","menu.settings":"Cài đặt",
    "charts.title":"Biểu đồ góc khớp theo thời gian","charts.subtitle":"Phiên đo gần nhất.",
    "charts.exercise":"Bài tập:","charts.patient_code":"Mã bệnh nhân:","charts.save_csv":"Lưu CSV",
    "charts.emg":"EMG","charts.next_ex":"Bài tập tiếp theo","charts.hip":"Hip (độ)","charts.knee":"Knee (độ)","charts.ankle":"Ankle (độ)",
    "charts.vas_title":"Đau chủ quan (VAS)","charts.vas_sub":"Mức đau trước và sau bài tập hiện tại (0–10).",
    "charts.vas_before":"Trước khi tập","charts.vas_after":"Sau khi tập","charts.vas_none":"Chưa ghi nhận",
    "charts.vas_more":"Đau tăng","charts.vas_more_desc_prefix":"Tăng khoảng","charts.vas_more_desc_suffix":"điểm sau bài tập.",
    "charts.vas_less":"Đau giảm","charts.vas_less_desc_prefix":"Giảm khoảng","charts.vas_less_desc_suffix":"điểm sau bài tập.",
    "charts.vas_same":"Không đổi","charts.vas_same_desc":"Mức đau không thay đổi sau bài tập.",
    "charts.vas_not_enough":"Chưa đủ dữ liệu VAS trước/sau cho bài này.","charts.vas_no_data":"Chưa ghi nhận VAS cho bài tập hiện tại.",
    "charts.fma_title":"Đánh giá FMA","charts.loading":"Đang xử lý...","charts.current_score":"Điểm bài hiện tại:","charts.all_ex_summary":"Tổng kết các bài đã đo",
    "charts.emg_title":"Tín hiệu điện cơ EMG","charts.emg_thigh":"Cơ đùi","charts.emg_good":"Khỏe","charts.emg_shank":"Cơ cẳng chân"
  },
  en: {
    "menu.title":"MENU","menu.home":"Home","menu.calib":"Calibration","menu.patinfo":"Patient info",
    "menu.record":"Records","menu.charts":"Charts","menu.settings":"Settings",
    "charts.title":"Joint angle chart over time","charts.subtitle":"Most recent session.",
    "charts.exercise":"Exercise:","charts.patient_code":"Patient code:","charts.save_csv":"Save CSV",
    "charts.emg":"EMG","charts.next_ex":"Next exercise","charts.hip":"Hip (deg)","charts.knee":"Knee (deg)","charts.ankle":"Ankle (deg)",
    "charts.vas_title":"Subjective pain (VAS)","charts.vas_sub":"Pain level before and after current exercise (0–10).",
    "charts.vas_before":"Before exercise","charts.vas_after":"After exercise","charts.vas_none":"No data",
    "charts.vas_more":"Higher pain","charts.vas_more_desc_prefix":"Increased by","charts.vas_more_desc_suffix":"points after the exercise.",
    "charts.vas_less":"Lower pain","charts.vas_less_desc_prefix":"Decreased by","charts.vas_less_desc_suffix":"points after the exercise.",
    "charts.vas_same":"No change","charts.vas_same_desc":"Pain level unchanged.",
    "charts.vas_not_enough":"Not enough VAS data for this exercise.","charts.vas_no_data":"No VAS data recorded.",
    "charts.fma_title":"FMA evaluation","charts.loading":"Processing...","charts.current_score":"Score of this exercise:","charts.all_ex_summary":"Summary of all exercises",
    "charts.emg_title":"EMG signal","charts.emg_thigh":"Thigh muscle","charts.emg_good":"Strong","charts.emg_shank":"Shank muscle"
  }
};
function applyLanguage(lang){
  const dict = I18N[lang] || I18N.vi;
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const key = el.getAttribute("data-i18n");
    if (dict[key]) el.textContent = dict[key];
  });
}
document.addEventListener("DOMContentLoaded", ()=>{
  const lang = localStorage.getItem("appLang") || "vi";
  applyLanguage(lang);
});
</script>

<script>
// ===== DATA FROM SERVER (Jinja) =====
const t_ms_raw    = {{ (t_ms    or []) | tojson }};
const hip_raw     = {{ (hip     or []) | tojson }};
const knee_raw    = {{ (knee    or []) | tojson }};
const ankle_raw   = {{ (ankle   or []) | tojson }};

const emg_raw     = {{ (emg     or []) | tojson }};
const emg_rms_raw = {{ (emg_rms or []) | tojson }};
const emg_env_raw = {{ (emg_env or []) | tojson }};

const currentExerciseName = {{ (exercise_name or '') | tojson }};
const patientCode         = {{ (patient_code  or '') | tojson }};
</script>

<script>
document.getElementById("btnToggleSB").onclick = () =>
  document.body.classList.toggle("sb-collapsed");

const CURRENT_LANG = localStorage.getItem("appLang") || "vi";

const TEXT = {
  vi: { no_ex_name:"Chưa có tên bài tập.", no_rom:"Không có dữ liệu ROM cho bài hiện tại.",
        no_ex_saved:"Chưa có bài nào được lưu.", total_all:"Tổng điểm các bài đã đo:",
        finished_all:"Đã hoàn thành các bài tập. Hệ thống sẽ quay lại trang đo." },
  en: { no_ex_name:"No exercise name.", no_rom:"No ROM data for the current exercise.",
        no_ex_saved:"No exercise has been saved yet.", total_all:"Total score of all exercises:",
        finished_all:"All exercises are completed. System will go back to measurement page." }
};

const STRENGTH_TEXT = {
  vi:{ good_label:"Tốt", good_desc:"Biên độ vận động lớn, kiểm soát động tác tốt.",
       mid_label:"Trung bình", mid_desc:"Biên độ vận động ở mức chấp nhận được, nên tiếp tục tập để cải thiện.",
       weak_label:"Yếu", weak_desc:"Biên độ vận động còn hạn chế, cần tăng cường tập luyện và theo dõi." },
  en:{ good_label:"Good", good_desc:"Large range of motion with good control.",
       mid_label:"Moderate", mid_desc:"Acceptable range of motion, further training is recommended.",
       weak_label:"Weak", weak_desc:"Limited range of motion, needs more training and follow-up." }
};

// ====== COPY RA BIẾN CHẠY (không dùng trực tiếp raw) ======
let t_ms     = (t_ms_raw    || []).slice();
let hipArr   = (hip_raw     || []).slice();
let kneeArr  = (knee_raw    || []).slice();
let ankleArr = (ankle_raw   || []).slice();

let emgArr    = (emg_raw     || []).slice();
let emgRmsArr = (emg_rms_raw || []).slice();
let emgEnvArr = (emg_env_raw || []).slice();

// ===== CLIP 6 GIÂY CUỐI (đồng bộ theo t_ms) =====
const WINDOW_MS = 6000;
(function clipLastWindow(){
  if (!t_ms.length) return;

  const lastT = t_ms[t_ms.length - 1];
  const minT  = lastT - WINDOW_MS;

  let startIdx = 0;
  while (startIdx < t_ms.length && t_ms[startIdx] < minT) startIdx++;

  if (startIdx > 0 && startIdx < t_ms.length) {
    t_ms     = t_ms.slice(startIdx);
    hipArr   = hipArr.slice(startIdx);
    kneeArr  = kneeArr.slice(startIdx);
    ankleArr = ankleArr.slice(startIdx);

    // emg arrays: nếu length khác t_ms, vẫn slice an toàn theo min length
    if (emgArr.length === t_ms_raw.length)    emgArr    = emgArr.slice(startIdx);
    if (emgRmsArr.length === t_ms_raw.length) emgRmsArr = emgRmsArr.slice(startIdx);
    if (emgEnvArr.length === t_ms_raw.length) emgEnvArr = emgEnvArr.slice(startIdx);
  }
})();

// ====== EXPORT DEBUG (gõ _dbg() trong console) ======
window._dbg = () => ({
  url: location.pathname,
  t_len: t_ms.length,
  hip_len: hipArr.length,
  emg_len: emgArr.length,
  emg_rms_len: emgRmsArr.length,
  emg_env_len: emgEnvArr.length,
  last_t: t_ms.length ? t_ms[t_ms.length-1] : null
});

// ====== CHART OPTIONS ======
const commonOptions = {
  responsive:true, maintainAspectRatio:false,
  interaction:{ mode:"index", intersect:false },
  plugins:{ legend:{ display:false }},
  scales:{
    x:{ title:{ display:true, text:"t (ms)" }},
    y:{ title:{ display:true, text:"Góc (°)" }, min:0, max:120 }
  }
};

function makeChart(canvasId, labels, yArr){
  const el = document.getElementById(canvasId);
  if (!el) return;
  new Chart(el, {
    type:"line",
    data:{ labels, datasets:[{ data:yArr, borderWidth:2, tension:0.15 }]},
    options: commonOptions
  });
}

makeChart("hipChart",   t_ms, hipArr);
makeChart("kneeChart",  t_ms, kneeArr);
makeChart("ankleChart", t_ms, ankleArr);

// ====== EMG CHART (3 đường) ======
(function makeEmgChart(){
  const el = document.getElementById("emgChart");
  if (!el) return;

  // Nếu không có EMG thì hiện chart rỗng (không crash)
  const hasAny = (emgArr && emgArr.length) || (emgRmsArr && emgRmsArr.length) || (emgEnvArr && emgEnvArr.length);
  const labels = t_ms;

  const ds = [];
  if (emgArr && emgArr.length) {
    ds.push({ label:"raw", data: emgArr, borderWidth:1.5, tension:0.15 });
  }
  if (emgRmsArr && emgRmsArr.length) {
    ds.push({ label:"rms", data: emgRmsArr, borderWidth:2, tension:0.15 });
  }
  if (emgEnvArr && emgEnvArr.length) {
    ds.push({ label:"env", data: emgEnvArr, borderWidth:2, tension:0.15 });
  }

  new Chart(el, {
    type: "line",
    data: { labels, datasets: ds },
    options: {
      responsive:true,
      maintainAspectRatio:false,
      interaction:{ mode:"index", intersect:false },
      plugins:{ legend:{ display:true }},
      scales:{
        x:{ title:{ display:true, text:"t (ms)" }},
        y:{ title:{ display:true, text:"EMG (a.u.)" } }
      }
    }
  });

  if (!hasAny) {
    console.warn("EMG is empty. Check route /charts to pass emg/emg_rms/emg_env.");
  }
})();

// ====== FMA (demo) ======
const evalBox = document.getElementById("evalContent");
const totalScoreSpan = document.getElementById("totalScore");

function fmaScore(rom){
  if (rom >= 90) return 2;
  if (rom >= 40 && rom <= 50) return 1;
  return 0;
}
function strengthInfo(score){
  score = Number(score) || 0;
  const T = STRENGTH_TEXT[CURRENT_LANG] || STRENGTH_TEXT.vi;
  if (score >= 2) return { label:T.good_label, desc:T.good_desc, badgeClass:"bg-success" };
  if (score === 1) return { label:T.mid_label,  desc:T.mid_desc,  badgeClass:"bg-warning text-dark" };
  return { label:T.weak_label, desc:T.weak_desc, badgeClass:"bg-danger" };
}

// ====== Scores localStorage ======
let storedScores = {};
try { storedScores = JSON.parse(localStorage.getItem("exerciseScores") || "{}"); }
catch(e){ storedScores = {}; }

const defaultOrder  = ["ankle flexion","knee flexion","hip flexion"];
const exerciseOrder = Array.from(new Set([...defaultOrder, ...Object.keys(storedScores)]));

function showCurrentExerciseScore(){
  const T = TEXT[CURRENT_LANG] || TEXT.vi;

  if (!currentExerciseName){
    evalBox.innerHTML = `<div class='text-muted'>${T.no_ex_name}</div>`;
    totalScoreSpan.textContent = "0 / 2";
    return;
  }

  const data = storedScores[currentExerciseName];

  if (!data){
    if (!kneeArr.length){
      evalBox.innerHTML = `<div class='text-muted'>${T.no_rom}</div>`;
      totalScoreSpan.textContent = "0 / 2";
      return;
    }
    const maxK = Math.max(...kneeArr);
    const minK = Math.min(...kneeArr);
    const rom  = maxK - minK;

    const score = fmaScore(rom);
    const info  = strengthInfo(score);

    evalBox.innerHTML = `
      <div class='eval-item'>
        <div class='strength-label mb-1'>${info.label}</div>
        <div class="fma-note-box p-3 my-2">
          <div class='strength-desc mb-0'>${info.desc}</div>
        </div>
      </div>`;
    totalScoreSpan.textContent = `${score} / 2`;
    totalScoreSpan.className = "badge ms-1 " + info.badgeClass;
    return;
  }

  const info = strengthInfo(data.score);
  evalBox.innerHTML = `
    <div class='eval-item'>
      <div class='strength-label mb-1'>${info.label}</div>
      <div class="fma-note-box p-3 my-2">
        <div class='strength-desc mb-0'>${info.desc}</div>
      </div>
    </div>`;
  totalScoreSpan.textContent = `${data.score} / 2`;
  totalScoreSpan.className = "badge ms-1 " + info.badgeClass;
}

const allSummaryDiv = document.getElementById("allExercisesSummary");

function renderAllExercisesSummary(){
  const T = TEXT[CURRENT_LANG] || TEXT.vi;
  if (!allSummaryDiv) return;

  const keys = Object.keys(storedScores);
  if (!keys.length){
    allSummaryDiv.innerHTML = `<div class='text-muted'>${T.no_ex_saved}</div>`;
    return;
  }

  let html = "";
  let total = 0;

  const sortedNames = [...keys].sort((a,b)=>{
    const ia = defaultOrder.indexOf(a), ib = defaultOrder.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  sortedNames.forEach((name, idx)=>{
    const d = storedScores[name];
    if (!d) return;
    total += d.score || 0;
    const info = strengthInfo(d.score);

    html += `
      <div class='eval-item'>
        <div class='d-flex justify-content-between align-items-center'>
          <div>
            <div class='fw-semibold'>${idx+1}. ${name}</div>
            <div class='small text-muted'>
              ROM Knee: ${(Number(d.romKnee||0)).toFixed(1)}°
              – <span class='strength-label'>${info.label}</span>
            </div>
          </div>
          <span class='eval-badge badge ${info.badgeClass}'>${d.score} / 2</span>
        </div>
      </div>`;
  });

  html += `
    <div class='total-summary'>
      ${T.total_all}
      <span>${total} / ${sortedNames.length * 2}</span>
    </div>`;
  allSummaryDiv.innerHTML = html;
}

showCurrentExerciseScore();
renderAllExercisesSummary();

// ===== Next exercise =====
document.getElementById("btnNextEx").onclick = () => {
  const T = TEXT[CURRENT_LANG] || TEXT.vi;
  const idx = exerciseOrder.indexOf(currentExerciseName);

  if (idx >= 0 && idx < exerciseOrder.length - 1){
    const nextName = exerciseOrder[idx + 1];
    let url = "/?next_ex=" + encodeURIComponent(nextName);
    if (patientCode) url += "&patient_code=" + encodeURIComponent(patientCode);
    window.location.href = url;
    return;
  }

  let url = "/";
  if (patientCode) url += "?patient_code=" + encodeURIComponent(patientCode);
  alert(T.finished_all);
  window.location.href = url;
};
</script>

</body>
</html>
"""
EMG_CHART_HTML = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title data-i18n="emg.page_title">Biểu đồ EMG</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root { --blue:#1669c9; --sbw:260px; }
body{ background:#e8f3ff; margin:0; }
.layout{ display:flex; gap:16px; position:relative; }

.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); transition:all .28s ease; }
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px; border-bottom-right-radius:16px;
  padding:16px; min-height:100vh;
}
.main-col{ flex:1 1 auto; min-width:0; }

body.sb-collapsed .sidebar-col{ flex-basis:0 !important; max-width:0 !important; }
body.sb-collapsed .sidebar{ padding:0 !important; }
body.sb-collapsed .sidebar *{ display:none; }

#btnToggleSB{
  border:2px solid #d8e6ff; background:#fff;
  border-radius:10px; padding:6px 10px; font-weight:700;
}
#btnToggleSB:hover{ background:#eef6ff; }

.menu-btn{
  width:100%; display:block; background:#1d74d8; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px;
  font-weight:600; text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; }
.menu-btn.active{ background:#0f5bb0; }

.panel{
  background:#fff; border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,0.10);
  padding:16px; margin-bottom:16px;
}
.chart-box{ height:420px; }
</style>
</head>

<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-3">
      <img src="/static/unnamed.png" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>
        <a class="menu-btn" href="/"                  data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn" href="/calibration"       data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage"   data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/charts"            data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn active" href="/charts_emg" data-i18n="menu.emg">Biểu đồ EMG</a>
        <a class="menu-btn" href="/settings"          data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <main class="main-col">
      <div class="panel">
        <div class="d-flex justify-content-between align-items-center">
          <div>
            <h5 data-i18n="emg.title">Biểu đồ tín hiệu EMG</h5>
            <div class="text-muted small" data-i18n="emg.subtitle">
              EMG (raw/RMS/envelope) theo thời gian – hiển thị 6 giây cuối của phiên đo gần nhất.
            </div>
          </div>
          <a class="btn btn-outline-primary btn-sm" href="/charts" data-i18n="emg.back">← Biểu đồ góc khớp</a>
        </div>
      </div>

      <div class="panel">
        <div class="chart-box">
          <canvas id="emgChart"></canvas>
        </div>
        <div class="small text-muted mt-2" id="hintBox"></div>
      </div>
    </main>
  </div>
</div>

<script>
// I18N cho trang EMG
const I18N_EMG = {
  vi:{
    "menu.title":"MENU","menu.home":"Trang chủ","menu.calib":"Hiệu chuẩn","menu.patinfo":"Thông tin bệnh nhân",
    "menu.charts":"Biểu đồ","menu.emg":"Biểu đồ EMG","menu.settings":"Cài đặt",
    "emg.title":"Biểu đồ tín hiệu EMG",
    "emg.subtitle":"EMG (raw/RMS/envelope) theo thời gian – hiển thị 6 giây cuối của phiên đo gần nhất.",
    "emg.back":"← Biểu đồ góc khớp"
  },
  en:{
    "menu.title":"MENU","menu.home":"Home","menu.calib":"Calibration","menu.patinfo":"Patient info",
    "menu.charts":"Angle charts","menu.emg":"EMG charts","menu.settings":"Settings",
    "emg.title":"EMG signal chart",
    "emg.subtitle":"EMG (raw/RMS/envelope) over time – show last 6 seconds of the latest session.",
    "emg.back":"← Joint angle charts"
  }
};
function applyLangEmg(lang){
  const dict = I18N_EMG[lang] || I18N_EMG.vi;
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k = el.getAttribute("data-i18n");
    if (dict[k]) el.textContent = dict[k];
  });
}
document.addEventListener("DOMContentLoaded", ()=>{
  const lang = localStorage.getItem("appLang") || "vi";
  document.documentElement.lang = lang;
  applyLangEmg(lang);
});
document.getElementById("btnToggleSB").onclick = () =>
  document.body.classList.toggle("sb-collapsed");
</script>

<script>
/* ===== DATA FROM SERVER (Jinja) ===== */
const t_ms_raw    = {{ (t_ms    or []) | tojson }};
const hip_raw     = {{ (hip     or []) | tojson }};
const knee_raw    = {{ (knee    or []) | tojson }};
const ankle_raw   = {{ (ankle   or []) | tojson }};

const emg_raw     = {{ (emg     or []) | tojson }};
const emg_rms_raw = {{ (emg_rms or []) | tojson }};
const emg_env_raw = {{ (emg_env or []) | tojson }};

/* ✅ EXPOSE TO WINDOW (để Console thấy) */
window.t_ms_raw = t_ms_raw;
window.hip_raw = hip_raw;
window.knee_raw = knee_raw;
window.ankle_raw = ankle_raw;

window.emg_raw = emg_raw;
window.emg_rms_raw = emg_rms_raw;
window.emg_env_raw = emg_env_raw;

/* ✅ DEBUG HELPER */
window._dbg = function(){
  return {
    t_len: (window.t_ms_raw||[]).length,
    hip_len: (window.hip_raw||[]).length,
    emg_len: (window.emg_raw||[]).length,
    rms_len: (window.emg_rms_raw||[]).length,
    env_len: (window.emg_env_raw||[]).length,
    t_first: (window.t_ms_raw||[])[0],
    t_last:  (window.t_ms_raw||[]).at ? (window.t_ms_raw||[]).at(-1) : (window.t_ms_raw||[])[(window.t_ms_raw||[]).length-1],
  };
};

/* ✅ LOG NGAY KHI LOAD (để khỏi phải gõ) */
console.log("[DBG] injected:", window._dbg());
</script>


<script>
// ===== CLIP 6s CUỐI =====
const WINDOW_MS = 6000;

let t_ms     = (t_ms_raw    || []).slice();
let emgArr   = (emg_raw     || []).slice();
let emgRms   = (emg_rms_raw || []).slice();
let emgEnv   = (emg_env_raw || []).slice();

(function clipLastWindow(){
  if (!t_ms.length) return;
  const lastT = t_ms[t_ms.length - 1];
  const minT  = lastT - WINDOW_MS;

  let startIdx = 0;
  while (startIdx < t_ms.length && t_ms[startIdx] < minT) startIdx++;

  if (startIdx > 0 && startIdx < t_ms.length) {
    t_ms = t_ms.slice(startIdx);

    // chỉ slice nếu length align với t_ms_raw (an toàn)
    if (emgArr.length === t_ms_raw.length) emgArr = emgArr.slice(startIdx);
    if (emgRms.length === t_ms_raw.length) emgRms = emgRms.slice(startIdx);
    if (emgEnv.length === t_ms_raw.length) emgEnv = emgEnv.slice(startIdx);
  }
})();

// ===== DEBUG HELPER (gõ _dbg() trong console) =====
window._dbg = () => ({
  url: location.pathname,
  t_len: t_ms.length,
  emg_len: emgArr.length,
  rms_len: emgRms.length,
  env_len: emgEnv.length,
  last_t: t_ms.length ? t_ms[t_ms.length-1] : null
});

// ===== DRAW CHART =====
const lang = localStorage.getItem("appLang") || "vi";
const AXIS = {
  vi: { x:"t (ms)", y:"EMG (a.u.)", empty:"Không có dữ liệu EMG. Kiểm tra route /charts_emg có truyền emg/emg_rms/emg_env và t_ms." },
  en: { x:"t (ms)", y:"EMG (a.u.)", empty:"No EMG data. Check /charts_emg passes emg/emg_rms/emg_env and t_ms." }
}[lang] || { x:"t (ms)", y:"EMG (a.u.)", empty:"No EMG data." };

const hintBox = document.getElementById("hintBox");

const datasets = [];
if (emgArr && emgArr.length) datasets.push({ label:"raw", data: emgArr, borderWidth:1.5, tension:0.15 });
if (emgRms && emgRms.length) datasets.push({ label:"rms", data: emgRms, borderWidth:2, tension:0.15 });
if (emgEnv && emgEnv.length) datasets.push({ label:"env", data: emgEnv, borderWidth:2, tension:0.15 });

if (!t_ms.length || !datasets.length) {
  hintBox.textContent = AXIS.empty;
  console.warn(AXIS.empty, window._dbg());
}

new Chart(document.getElementById("emgChart"), {
  type:"line",
  data:{ labels: t_ms, datasets },
  options:{
    responsive:true, maintainAspectRatio:false,
    interaction:{ mode:"index", intersect:false },
    plugins:{ legend:{ display:true } },
    scales:{
      x:{ title:{ display:true, text: AXIS.x } },
      y:{ title:{ display:true, text: AXIS.y } }
    }
  }
});
</script>

</body>
</html>
"""


# ===================== Patients Manage =====================
PATIENTS_MANAGE_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thông tin bệnh nhân</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{ --blue:#1669c9; --sbw:260px; }
body{ background:#e8f3ff; }

/* Bố cục & sidebar giống Trang chủ / Hiệu chuẩn */
.layout{ display:flex; gap:16px; position:relative; }
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px; border-bottom-right-radius:16px;
  padding:16px; width:var(--sbw); min-height:100vh;
  box-sizing:border-box;
}
.sidebar-col{
  flex:0 0 var(--sbw);
  max-width:var(--sbw);
  transition:flex-basis .28s ease, max-width .28s ease, transform .28s ease;
  will-change:flex-basis,max-width,transform;
}
.main-col{ flex:1 1 auto; min-width:0; }

/* Mặc định THU GỌN (ẩn sidebar) */
.sb-collapsed .sidebar-col{ flex-basis:0; max-width:0; transform:translateX(-8px); }
.sb-collapsed .sidebar{ padding:0; width:0; border-radius:0; }
.sb-collapsed .sidebar *{ display:none; }

/* Nút ☰ trên navbar */
#btnToggleSB{
  border:2px solid #d8e6ff; border-radius:10px; background:#fff;
  padding:6px 10px; font-weight:700;
}
#btnToggleSB:hover{ background:#f4f8ff; }

/* Card / form */
.card{ border-radius:14px; box-shadow:0 8px 18px rgba(16,24,40,.06) }
.form-label{ font-weight:600; color:#244e78 }
.btn-outline-thick{ border:2px solid #151515; border-radius:12px; background:#fff; font-weight:600; }
.table thead th{ background:#eef5ff; color:#083a6a }
.input-sm{ height:36px; }

/* Menu trong sidebar */
.menu-btn{
  width:100%; display:block; background:#1973d4; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600;
  text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; color:#fff }
.menu-btn.active{ background:#0f5bb0; }
</style>
</head>
<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0" data-i18n="patients.manage_title">Thông tin bệnh nhân</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="40">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold" data-i18n="menu.title">MENU</div>
        <a class="menu-btn" href="/"                 data-i18n="menu.home">Trang chủ</a>
        <a class="menu-btn" href="/calibration"      data-i18n="menu.calib">Hiệu chuẩn</a>
        <a class="menu-btn active" href="/patients/manage" data-i18n="menu.patinfo">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/records"          data-i18n="menu.record">Bệnh án</a>
        <a class="menu-btn" href="/charts"           data-i18n="menu.charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings"         data-i18n="menu.settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="row g-3">
        <!-- Form trái -->
        <div class="col-lg-5">
          <div class="card p-3">
            <div class="row g-3">
              <div class="col-12">
                <label class="form-label" data-i18n="patients.name">Họ và tên</label>
                <input id="name" class="form-control input-sm">
              </div>
              <div class="col-12">
                <label class="form-label" data-i18n="patients.id">CCCD</label>
                <input id="national_id" class="form-control input-sm">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="patients.dob">Ngày sinh</label>
                <input id="dob" class="form-control input-sm" placeholder="vd 30/05/2001 hoặc 2001-05-30">
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="patients.gender">Giới tính</label>
                <select id="gender" class="form-select input-sm">
                  <option value="">--</option>
                  <option>Male</option>
                  <option>Female</option>
                </select>
              </div>
              <div class="col-6">
                <label class="form-label" data-i18n="patients.height">Chiều cao (cm)</label>
                <input id="height" class="form-control input-sm">
              </div>
              <input type="hidden" id="pat_code">
              <div class="col-6">
                <label class="form-label" data-i18n="patients.weight">Cân nặng (kg)</label>
                <input id="weight" class="form-control input-sm">
              </div>

              <div class="col-12">
                <label class="form-label" data-i18n="patients.code">Mã bệnh nhân</label>
                <input id="patient_code" class="form-control input-sm" placeholder="(để trống để tạo mới)">
              </div>

              <div class="col-12 d-flex justify-content-center gap-4 mt-2">
                <button id="btnSave" class="btn btn-outline-thick py-2 px-5 fs-5" data-i18n="patients.save">Lưu</button>
                <button id="btnDelete" class="btn btn-outline-thick py-2 px-5 fs-5" data-i18n="patients.delete">Xóa</button>
              </div>
            </div>
          </div>

          <div class="card p-3 mt-3">
            <button id="btnClearAll" class="btn btn-outline-danger w-100" data-i18n="patients.clear_all">Xóa toàn bộ danh sách</button>
          </div>
        </div>

        <!-- Bảng phải -->
        <div class="col-lg-7">
          <div class="card p-3">
            <input id="q" class="form-control mb-3" placeholder="Tìm kiếm..." data-i18n-placeholder="patients.search">
            <div class="table-responsive">
              <table class="table table-hover align-middle" id="tbl">
                <thead>
                  <tr>
                    <th style="width:60px">#</th>
                    <th data-i18n="patients.th_code">Mã bệnh nhân</th>
                    <th data-i18n="patients.th_name">Họ và tên</th>
                    <th data-i18n="patients.th_id">CCCD</th>
                    <th data-i18n="patients.th_dob">Ngày sinh</th>
                    <th data-i18n="patients.th_gender">Giới tính</th>
                  </tr>
                </thead>
                <tbody></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </main>
  </div>
</div>

<script>
// I18N cho trang quản lý bệnh nhân
const I18N_PAT = {
  vi:{
    "menu.title":"MENU",
    "menu.home":"Trang chủ",
    "menu.calib":"Hiệu chuẩn",
    "menu.patinfo":"Thông tin bệnh nhân",
    "menu.record":"Bệnh án",
    "menu.charts":"Biểu đồ",
    "menu.settings":"Cài đặt",

    "patients.manage_title":"Thông tin bệnh nhân",
    "patients.name":"Họ và tên",
    "patients.id":"CCCD",
    "patients.dob":"Ngày sinh",
    "patients.gender":"Giới tính",
    "patients.height":"Chiều cao (cm)",
    "patients.weight":"Cân nặng (kg)",
    "patients.code":"Mã bệnh nhân",
    "patients.save":"Lưu",
    "patients.delete":"Xóa",
    "patients.clear_all":"Xóa toàn bộ danh sách",
    "patients.search":"Tìm kiếm...",
    "patients.th_code":"Mã bệnh nhân",
    "patients.th_name":"Họ và tên",
    "patients.th_id":"CCCD",
    "patients.th_dob":"Ngày sinh",
    "patients.th_gender":"Giới tính"
  },
  en:{
    "menu.title":"MENU",
    "menu.home":"Home",
    "menu.calib":"Calibration",
    "menu.patinfo":"Patient info",
    "menu.record":"Records",
    "menu.charts":"Charts",
    "menu.settings":"Settings",

    "patients.manage_title":"Patient information",
    "patients.name":"Full name",
    "patients.id":"National ID",
    "patients.dob":"Date of birth",
    "patients.gender":"Gender",
    "patients.height":"Height (cm)",
    "patients.weight":"Weight (kg)",
    "patients.code":"Patient code",
    "patients.save":"Save",
    "patients.delete":"Delete",
    "patients.clear_all":"Delete all",
    "patients.search":"Search...",
    "patients.th_code":"Patient code",
    "patients.th_name":"Full name",
    "patients.th_id":"ID",
    "patients.th_dob":"Date of birth",
    "patients.th_gender":"Gender"
  }
};
function applyLangPatients(lang){
  const dict = I18N_PAT[lang] || I18N_PAT.vi;
  document.querySelectorAll("[data-i18n]").forEach(el=>{
    const k = el.getAttribute("data-i18n");
    if (dict[k]) el.textContent = dict[k];
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach(el=>{
    const k = el.getAttribute("data-i18n-placeholder");
    if (dict[k]) el.placeholder = dict[k];
  });
}

// Toggle sidebar: giống các trang khác
document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* ===== Logic quản lý bệnh nhân ===== */
let DATA = {rows:[], raw:{}};
const $ = (id)=>document.getElementById(id);

function loadAll(){
  fetch('/api/patients').then(r=>r.json()).then(d=>{
    DATA = d; renderTable(d.rows);
  });
}
function renderTable(rows){
  const tb = document.querySelector('#tbl tbody');
  tb.innerHTML = '';
  rows.forEach((r,i)=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${r.code||''}</td>
      <td>${r.full_name||''}</td>
      <td>${r.national_id||''}</td>
      <td>${r.dob||''}</td>
      <td>${r.sex||''}</td>`;
    tr.onclick = ()=>fillFormFromRow(r.code);
    tb.appendChild(tr);
  });
}
function fillFormFromRow(code){
  const rec = DATA.raw[code] || {};
  $('patient_code').value = rec.PatientCode || '';
  $('name').value        = rec.name || '';
  $('national_id').value = rec.ID || '';
  $('dob').value         = rec.DateOfBirth || '';
  $('gender').value      = rec.Gender || '';
  $('height').value      = rec.Height || '';
  $('weight').value      = rec.Weight || '';
}
$('q').addEventListener('input', ()=>{
  const kw = $('q').value.toLowerCase();
  const rows = DATA.rows.filter(r =>
    (r.code||'').toLowerCase().includes(kw) ||
    (r.full_name||'').toLowerCase().includes(kw) ||
    (r.national_id||'').toLowerCase().includes(kw)
  );
  renderTable(rows);
});
$('btnSave').onclick = ()=>{
  const payload = {
    patient_code: $('patient_code').value.trim(),
    name:         $('name').value.trim(),
    national_id:  $('national_id').value.trim(),
    dob:          $('dob').value.trim(),
    gender:       $('gender').value,
    height:       $('height').value.trim(),
    weight:       $('weight').value.trim(),
  };
  fetch('/api/patients', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  }).then(r=>r.json()).then(res=>{
    if(res.ok){ alert('Đã lưu!'); loadAll(); $('patient_code').value = res.patient_code; }
    else{ alert(res.msg||'Lỗi'); }
  });
};
$('btnDelete').onclick = ()=>{
  const code = $('patient_code').value.trim();
  if(!code){ alert('Chọn/nhập mã bệnh nhân'); return; }
  if(!confirm('Xóa bệnh nhân này?')) return;
  fetch('/api/patients/'+encodeURIComponent(code), {method:'DELETE'})
    .then(r=>r.json()).then(res=>{
      if(res.ok){ alert('Đã xóa'); loadAll(); }
      else alert(res.msg||'Lỗi');
    });
};
$('btnClearAll').onclick = ()=>{
  if(!confirm('Xóa TOÀN BỘ danh sách?')) return;
  fetch('/api/patients', {method:'DELETE'})
    .then(r=>r.json()).then(res=>{
      if(res.ok){ alert('Đã xóa toàn bộ'); loadAll(); }
    });
};
loadAll();

// áp dụng ngôn ngữ sau khi DOM sẵn sàng
document.addEventListener("DOMContentLoaded", ()=>{
  const lang = localStorage.getItem("appLang") || "vi";
  applyLangPatients(lang);
});
</script>
</body></html>
"""



@app.route("/save_patient", methods=["POST"])
def save_patient():
    data = request.get_json(force=True) or {}
    code = data.get("code") or f"BN{int(time.time())}"
    if fs_client is None:
        return {"ok": True, "code": code, "note": "Firestore disabled (local mode)"}
    try:
        fs_client.collection("patients").document(code).set(data)
        return {"ok": True, "code": code}
    except Exception as e:
        print("Lỗi khi lưu Firestore:", e)
        return {"ok": False, "error": str(e)}, 500


def stop_serial_reader():
    global stop_serial_thread, ser, serial_thread
    stop_serial_thread = True
    try:
        if ser and ser.is_open:
            ser.close()
    except:
        pass
    ser = None
    # chờ thread dừng (nhanh)
    if serial_thread and serial_thread.is_alive():
        try:
            serial_thread.join(timeout=1.0)
        except:
            pass
    serial_thread = None


_last = {"hip": None, "knee": None, "ankle": None}
ALPHA = 0.3


def _smooth(key, val):
    global _last
    if _last[key] is None:
        _last[key] = val
    else:
        _last[key] = _last[key] * (1 - ALPHA) + val * ALPHA
    return _last[key]


@app.post("/api/imu")  # <— ĐẶT NGAY TRƯỚC HÀM
def api_receive_imu():
    data = request.get_json(force=True) or {}
    p1, p2, p3, p4 = [data.get(k) for k in ("p1", "p2", "p3", "p4")]
    if None in (p1, p2, p3, p4):
        return {"ok": False, "msg": "Thiếu dữ liệu"}, 400

    # --- Giới hạn góc hợp lý theo sinh học ---
    def clamp_local(val, lo, hi):
        return max(lo, min(hi, val))

    raw_hip = norm_deg(p2 - p1)
    raw_knee = norm_deg(p3 - p2)
    raw_ankle = norm_deg(p4 - p3)
    hip = clamp_local(raw_hip, -40, 140)
    knee = clamp_local(raw_knee, -10, 160)
    ankle = clamp_local(raw_ankle, 0, 100)

    # --- Làm mượt ---
    hip = _smooth("hip", hip)
    knee = _smooth("knee", knee)
    ankle = _smooth("ankle", ankle)

    append_samples([{
        "t_ms": data.get("t_ms", time.time() * 1000),
        "hip": hip, "knee": knee, "ankle": ankle
    }])
    return {"ok": True}


# ===================== Run =====================
if __name__ == "__main__":
    socketio.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PORT", 8080)),
        debug=True,
        allow_unsafe_werkzeug=True
    )





