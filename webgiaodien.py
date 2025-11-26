# webgiaodien.py
import os, json, time
from datetime import datetime
import threading
from collections import defaultdict
import io, csv
from flask import send_file  # thêm import này
from flask import render_template_string

data_buffer = []  # bộ đệm mẫu đo
LAST_SESSION = []
DATA_LOCK = threading.Lock()

# Bật/tắt đọc cổng COM khi chạy local
SERIAL_ENABLED = True  # ép bật serial

MAX_LOCK = threading.Lock()
MAX_ANGLES = {"hip": 0.0, "knee": 0.0, "ankle": 0.0}

# ==== STATE & NGƯỠNG CHO HIP DÙNG PITCH2 ====
HIP_STATE    = {"mode": "front", "prev_pitch2": 0.0}  # mode: 'front' hoặc 'back'
PITCH_MID    = 90.0    # pitch2 ~ 90° là “biên” giữa trước / sau
PITCH_HYS    = 10.0    # hysteresis: <80° chắc chắn là front, >100° chắc chắn là back
HIP_CROSS_TH = 40.0    # chỉ đổi mode khi |hip thô| < 40°
DEADZONE     = 2.0     # |hip| < 2° coi như 0 cho mượt
# ============================================

def reset_max_angles():
    with MAX_LOCK:
        MAX_ANGLES["hip"] = 0.0
        MAX_ANGLES["knee"] = 0.0
        MAX_ANGLES["ankle"] = 0.0


# Dùng alias để tránh đè tên
pyserial = None
list_ports = None
try:
    if SERIAL_ENABLED:
        import serial as pyserial
        from serial.tools import list_ports
except Exception:
    SERIAL_ENABLED = False  # fallback


def auto_detect_port():
    if not list_ports:
        return None
    ports = list(list_ports.comports())
    for p in ports:
        if any(x in (p.description or "").upper() for x in ["USB", "ACM", "CP210", "CH340", "UART", "SERIAL"]):
            return p.device
    return ports[0].device if ports else None


try:
    if SERIAL_ENABLED:
        import serial, serial.tools.list_ports  # cần pyserial
    else:
        serial = None
except Exception:
    serial = None
    SERIAL_ENABLED = False
ser = None
serial_thread = None
stop_serial_thread = False


# ==== Helpers toàn cục ====
def norm_deg(x: float) -> float:
    while x > 180:
        x -= 360
    while x < -180:
        x += 360
    return x


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def start_serial_reader(port="COM5", baud=115200):
    """Đọc dữ liệu serial: id,timestamp,yaw,roll,pitch (4 IMU, dùng pitch)."""
    global ser, serial_thread, stop_serial_thread

    if not port:
        print("Không tìm thấy cổng serial nào.")
        return False

    try:
        ser = pyserial.Serial(port, baud, timeout=0.5)
        print(f" Đã mở {port} @ {baud}")
    except Exception as e:
        print("Không mở được cổng serial:", e)
        return False

    stop_serial_thread = False
    last_angles = defaultdict(lambda: {"yaw": 0.0, "roll": 0.0, "pitch": 0.0, "ts": 0.0})

    def norm_deg(x: float) -> float:
        while x > 180: x -= 360
        while x < -180: x += 360
        return x

    def reader_loop():
        print(f" Đang đọc dữ liệu từ {port} @ {baud} ...")
        import re
        CSV_PAT = re.compile(
            r'^\s*(-?\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)\s*$'
        )

        while not stop_serial_thread:
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                # Lọc rác: chỉ nhận đúng CSV 5 số
                m = CSV_PAT.match(line)
                if not m:
                    continue

                sid = int(float(m.group(1)))
                ts = float(m.group(2))
                yaw = float(m.group(3))
                roll = float(m.group(4))
                pitch = float(m.group(5))

                last_angles[sid] = {
                    "yaw": yaw, "roll": roll, "pitch": pitch, "ts": ts
                }

                # Cho hiển thị tạm khi có >=2 IMU (test), đủ 1-4 thì lấy tương ứng
                p1 = last_angles.get(1, {}).get("roll", 0.0)
                p2 = last_angles.get(2, {}).get("roll", 0.0)
                p3 = last_angles.get(3, {}).get("roll", 0.0)
                p4 = -last_angles.get(4, {}).get("roll", 0.0)
                pitch2 = last_angles.get(2, {}).get("pitch", 0.0)  # ⭐ pitch của IMU2
                # Góc thô (chưa xử lý đổi hướng hip)
                raw_hip   = norm_deg(p2 - p1)
                raw_knee  = norm_deg(p3 - p2)
                raw_ankle = norm_deg(p4 - p3)

                # Gửi cả p2 để xử lý đổi dấu ở append_samples
                append_samples([{
                    "t_ms": ts or time.time() * 1000,
                    "hip":   raw_hip,
                    "knee":  raw_knee,
                    "ankle": raw_ankle,
                    "p2":    p2,
                    "pitch2": pitch2
                }])


            except Exception as e:
                print("Serial read error:", e)

        print(" Dừng đọc serial")

    serial_thread = threading.Thread(target=reader_loop, daemon=True)
    serial_thread.start()
    return True


from flask import Flask, render_template_string, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO

# ================= Firebase Admin SDK =================
import firebase_admin
from firebase_admin import credentials, firestore


def find_firebase_key():
    candidates = [
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        "/etc/secrets/firebase-key.json",
        os.path.join(os.environ.get("RENDER_SECRETS_DIR", ""), "firebase-key.json"),
        os.path.join(os.getcwd(), "firebase-key.json"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


fs_client = None
try:
    CRED_PATH = find_firebase_key()
    if CRED_PATH:
        cred = credentials.Certificate(CRED_PATH)
        firebase_admin.initialize_app(cred)
        fs_client = firestore.client()
        print(" Firebase initialized")
    else:
        print("ℹ  Firebase key not found → chạy local không dùng Firestore")
except Exception as e:
    print("  Firebase init skipped:", e)
    fs_client = None

# ===================== App & Auth =====================
app = Flask(__name__)
app.secret_key = "CHANGE_ME"  # nhớ đổi khi deploy
PATIENTS_FILE = "sample.json"
EXPORT_DIR = "exports"
os.makedirs(EXPORT_DIR, exist_ok=True)


# chỗ khởi tạo SocketIO
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    ping_interval=10,  # giây
    ping_timeout=30,  # giây
    async_mode="threading",
)
from flask_socketio import emit


@socketio.on('connect')
def _on_connect():
    print('[SOCKET] client connected')
    emit('imu_data', {
        "t": time.time() * 1000,
        "hip": 0,
        "knee": 0,
        "ankle": 0
    })


@app.post("/session/mock")
@login_required
def session_mock():
    for i in range(30):
        append_samples([{
            "t_ms": time.time() * 1000,
            "hip": 10 + i * 0.5,
            "knee": 20 + i * 0.3,
            "ankle": -5 + i * 0.2,
        }])
        time.sleep(0.1)
    return {"ok": True, "mode": "mock"}


def append_samples(samples):
    global data_buffer, HIP_STATE

    for s in samples:
        t_ms = s.get("t_ms", time.time() * 1000)

        # Góc thô từ reader_loop
        raw_hip = float(s.get("hip", 0.0))
        knee    = float(s.get("knee", 0.0))
        ankle   = float(s.get("ankle", 0.0))

        p2      = float(s.get("p2", 0.0))
        pitch2  = float(s.get("pitch2", 0.0))

        # ====== DÙNG pitch2 ĐỂ CHỌN HƯỚNG HIP (với hysteresis + biên độ) ======
        mode        = HIP_STATE.get("mode", "front")   # 'front' hoặc 'back'
        prev_pitch2 = HIP_STATE.get("prev_pitch2", 0.0)

        # Chỉ cho phép đổi mode khi chân gần thẳng (|raw_hip| nhỏ)
        if abs(raw_hip) < HIP_CROSS_TH:
            # pitch2 thấp hẳn → chắc chắn đang gập ra TRƯỚC
            if pitch2 <= (PITCH_MID - PITCH_HYS):
                mode = "front"
            # pitch2 cao hẳn → chắc chắn đang gập ra SAU
            elif pitch2 >= (PITCH_MID + PITCH_HYS):
                mode = "back"
            # nếu pitch2 nằm giữa [80,100] thì giữ nguyên mode cũ, tránh nhảy liên tục

        HIP_STATE["mode"]        = mode
        HIP_STATE["prev_pitch2"] = pitch2

        sign_front = 1 if mode == "front" else -1

        # Biên độ hip + deadzone quanh 0 cho mượt
        mag_hip = abs(raw_hip)
        if mag_hip < DEADZONE:
            hip = 0.0
        else:
            hip = sign_front * mag_hip

        # ====== CLAMP ======
        hip   = clamp(hip,  -30.1, 122.1)
        knee  = clamp(abs(knee),   0, 134)
        ankle = clamp(abs(ankle), 36, 113)

        # ====== LÀM MƯỢT ======
        hip   = _smooth("hip", hip)
        knee  = _smooth("knee", knee)
        ankle = _smooth("ankle", ankle)

        # ====== CẬP NHẬT MAX ======
        with MAX_LOCK:
            if hip   > MAX_ANGLES["hip"]:   MAX_ANGLES["hip"]   = hip
            if knee  > MAX_ANGLES["knee"]:  MAX_ANGLES["knee"]  = knee
            if ankle > MAX_ANGLES["ankle"]: MAX_ANGLES["ankle"] = ankle

            max_payload = {
                "maxHip":   MAX_ANGLES["hip"],
                "maxKnee":  MAX_ANGLES["knee"],
                "maxAnkle": MAX_ANGLES["ankle"],
            }

        # ====== LƯU BUFFER ======
        with DATA_LOCK:
            data_buffer.append({
                "t_ms": t_ms,
                "hip":  hip,
                "knee": knee,
                "ankle": ankle
            })

        # ====== EMIT RA UI ======
        socketio.emit("imu_data", {
            "t": t_ms,
            "hip": hip,
            "knee": knee,
            "ankle": ankle,
            **max_payload
        })





login_manager = LoginManager(app)
login_manager.login_view = "login"

USERS = {"komlab": generate_password_hash("123456")}  # đổi khi deploy

# Map bài tập -> đường dẫn video (trong static/videos/)
EXERCISE_VIDEOS = {
    "ankle flexion": "/static/videos/ankle flexion.mp4",
    "hip flexion": "/static/videos/hip flexion.mp4",
    "knee flexion": "/static/knee flexion.mp4",
}


class User(UserMixin):
    def __init__(self, u): self.id = u


@login_manager.user_loader
def load_user(u): return User(u) if u in USERS else None


# ===================== Patient helpers =====================
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


def add_patient_to_file(full_name, national_id, dob, sex, weight, height):
    rows, raw = load_patients_rows()
    patient_code = gen_patient_code(full_name)

    g = (sex or "").strip()
    if g.lower().startswith("m"):
        g = "Male"
    elif g.lower().startswith("f"):
        g = "FeMale"

    raw[patient_code] = {
        "DateOfBirth": dob or "",
        "Exercise": {},
        "Gender": g,
        "Height": height or "",
        "ID": national_id or "",
        "PatientCode": patient_code,
        "Weight": weight or "",
        "name": full_name
    }
    with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    return patient_code


def gen_patient_code(full_name: str) -> str:
    last = (full_name.split()[-1] if full_name else "BN")
    base = "".join(ch for ch in last if ch.isalnum())
    suffix = datetime.now().strftime("%m%d%H%M")
    return f"{base}{suffix}"


# ===================== Routes =====================
@app.route("/login", methods=["GET", "POST"])
def login():
    error_message = None

    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")

        if u in USERS and check_password_hash(USERS[u], p):
            login_user(User(u))
            return redirect(url_for("dashboard"))
        else:
            # Sai tài khoản hoặc mật khẩu → gửi xuống HTML
            error_message = "Sai tài khoản hoặc mật khẩu"

    return render_template_string(LOGIN_HTML, error_message=error_message)
@app.route("/register", methods=["POST"])
def register():
    username = request.form.get("reg_username", "").strip()
    pw1      = request.form.get("reg_password", "")
    pw2      = request.form.get("reg_password2", "")

    if not username or not pw1:
        # thiếu dữ liệu → quay lại trang login
        flash("Vui lòng nhập đầy đủ tài khoản và mật khẩu", "danger")
        return redirect(url_for("login"))

    if pw1 != pw2:
        flash("Mật khẩu nhập lại không khớp", "danger")
        return redirect(url_for("login"))

    global USERS
    if username in USERS:
        flash("Tài khoản đã tồn tại", "danger")
        return redirect(url_for("login"))

    USERS[username] = generate_password_hash(pw1)
    flash("Đăng ký thành công, vui lòng đăng nhập", "success")
    return redirect(url_for("login"))


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASH_HTML, username=current_user.id, videos=EXERCISE_VIDEOS)


@app.post("/session/start")
@login_required
def session_start():
    global data_buffer
    data_buffer = []
    print(f"[SESSION] SERIAL_ENABLED={SERIAL_ENABLED}")
    if SERIAL_ENABLED:
        port = "COM5"
        baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        print(f"[SESSION] will open port={port} baud={baud}")
        ok = start_serial_reader(port=port, baud=baud)
        print(f"[SESSION] start_serial_reader ok={ok}")
        if not ok:
            return {"ok": False, "msg": f"Không mở được cổng serial (port={port})"}, 500
        return {"ok": True, "mode": "serial", "port": port, "baud": baud}
    else:
        print("[SESSION] SERIAL is DISABLED → noserial mode")
        return {"ok": True, "mode": "noserial"}


@app.get("/session/export_csv")
@login_required
def session_export_csv():
    """
    Xuất CSV cho phiên đo:
      - Nếu đã bấm KẾT THÚC ĐO → dùng LAST_SESSION
      - Nếu chưa kết thúc mà bấm export → dùng data_buffer
      - Nếu có patient_code → gắn vào tên file + lưu link vào JSON bệnh nhân
    """
    global LAST_SESSION

    patient_code = request.args.get("patient_code", "").strip()

    with DATA_LOCK:
        if LAST_SESSION:
            rows = list(LAST_SESSION)   # phiên đo gần nhất
        else:
            rows = list(data_buffer)    # dữ liệu đang đo (fallback)

    if not rows:
        rows = []

    # Tạo CSV text
    sio = io.StringIO()
    w = csv.writer(sio)
    w.writerow(["t_ms", "hip_deg", "knee_deg", "ankle_deg"])
    for r in rows:
        w.writerow([
            int(r.get("t_ms", 0)),
            f'{float(r.get("hip",   0)):.4f}',
            f'{float(r.get("knee",  0)):.4f}',
            f'{float(r.get("ankle", 0)):.4f}',
        ])

    csv_text = sio.getvalue()
    data = io.BytesIO(csv_text.encode("utf-8-sig"))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # sanitize mã bệnh nhân để đưa vào tên file
    safe_code = "".join(ch for ch in patient_code if ch.isalnum() or ch in ("-", "_"))
    if safe_code:
        filename = f"{safe_code}_{ts}_{len(rows)}rows.csv"
    else:
        filename = f"imu_{ts}_{len(rows)}rows.csv"

    #  Lưu file vật lý vào thư mục exports/
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        disk_path = os.path.join(EXPORT_DIR, filename)
        with open(disk_path, "w", encoding="utf-8-sig", newline="") as f:
            f.write(csv_text)

        #  Nếu có patient_code thì lưu link file vào JSON bệnh nhân
        if patient_code:
            _ensure_patients_file()
            with open(PATIENTS_FILE, "r", encoding="utf-8") as f:
                pdata = json.load(f) or {}

            rec = pdata.get(patient_code)
            if rec is not None:
                ex = rec.get("Exercise") or {}
                key = ts  # mỗi lần export 1 key mới theo timestamp
                ex[key] = {
                    "csv_file": disk_path,
                    "export_time": ts,
                    "n_samples": len(rows),
                }
                rec["Exercise"] = ex
                pdata[patient_code] = rec

                with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(pdata, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Không lưu được CSV vật lý hoặc cập nhật JSON:", e)
        # vẫn trả file CSV xuống cho user, chỉ là không lưu được metadata

    data.seek(0)
    return send_file(
        data,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )



@app.post("/session/stop")
@login_required
def session_stop():
    global LAST_SESSION, data_buffer

    # nếu đang đọc serial thì dừng
    if SERIAL_ENABLED:
        stop_serial_reader()

    #  LƯU LẠI PHIÊN ĐO GẦN NHẤT ĐỂ VẼ BIỂU ĐỒ
    LAST_SESSION = list(data_buffer)  # clone mảng
    print(f"[SESSION STOP] saved {len(LAST_SESSION)} samples")

    # xóa buffer để không bị lẫn vào lần đo sau
    data_buffer.clear()

    return {"ok": True, "msg": "Đã kết thúc phiên đo"}


@app.post("/session/reset_max")
@login_required
def session_reset_max():
    reset_max_angles()
    # Phát lại max=0 để UI cập nhật ngay
    socketio.emit("imu_data", {
        "t": time.time() * 1000,
        "hip": None, "knee": None, "ankle": None,
        "maxHip": 0.0, "maxKnee": 0.0, "maxAnkle": 0.0
    })
    return {"ok": True}


@app.route("/patients")
@login_required
def patients_list():
    rows, _ = load_patients_rows()
    return render_template_string(PATIENTS_LIST_HTML, rows=rows)


@app.route("/patients/new", methods=["GET", "POST"])
@login_required
def patients_new():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        national_id = request.form.get("national_id", "").strip()
        dob = request.form.get("dob", "").strip()
        sex = request.form.get("sex", "").strip()
        weight = request.form.get("weight", "").strip()
        height = request.form.get("height", "").strip()

        if not full_name:
            flash("Vui lòng nhập Họ và tên", "danger")
            return render_template_string(PATIENT_NEW_HTML)

        code = add_patient_to_file(full_name, national_id, dob, sex, weight, height)
        flash(f"Đã lưu bệnh nhân mới: {code}", "success")
        return redirect(url_for("patients_list"))
    return render_template_string(PATIENT_NEW_HTML)


@app.route("/patients/manage")
@login_required
def patients_manage():
    return render_template_string(PATIENTS_MANAGE_HTML)


@app.route("/ports")
@login_required
def ports():
    if not list_ports:
        return {"ports": []}
    items = [{"device": p.device, "desc": p.description} for p in list_ports.comports()]
    return {"ports": items}


@app.get("/api/patients")
@login_required
def api_patients_all():
    rows, raw = load_patients_rows()
    return {"rows": rows, "raw": raw}


@app.post("/api/patients")
@login_required
def api_patients_save():
    data = request.json or {}
    code = (data.get("patient_code") or "").strip()
    full_name = (data.get("name") or "").strip()
    if not full_name:
        return {"ok": False, "msg": "Thiếu họ tên"}, 400

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
    return {"ok": True, "patient_code": code}


@app.delete("/api/patients/<code>")
@login_required
def api_patients_delete(code):
    _, raw = load_patients_rows()
    if code in raw:
        raw.pop(code)
        with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        return {"ok": True}
    return {"ok": False, "msg": "Không tìm thấy"}, 404


@app.delete("/api/patients")
@login_required
def api_patients_clear_all():
    with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=2)
    return {"ok": True}


# ====== NEW: Trang Hiệu chuẩn kiểu lưới như ảnh ======
@app.route("/calibration")
@login_required
def calibration():
    open_guide = request.args.get("guide", "0") in ("1", "true", "yes")
    return render_template_string(CALIBRATION_HTML, username=current_user.id, open_guide=open_guide)


@app.route("/charts")
@login_required
def charts():
    global LAST_SESSION

    patient_code   = request.args.get("patient_code", "").strip()
    exercise_name  = request.args.get("exercise", "").strip()  # 🔹 tên bài tập hiện tại

    # Khi chưa có phiên đo
    if not LAST_SESSION:
        return render_template_string(
            CHARTS_HTML,
            username=current_user.id,
            t_ms=[],
            hip=[],
            knee=[],
            ankle=[],
            patient_code=patient_code,
            exercise_name=exercise_name,
        )

    rows = LAST_SESSION[:]
    rows.sort(key=lambda x: x["t_ms"])

    raw_t    = [r["t_ms"] for r in rows]
    hipArr   = [r["hip"]   for r in rows]
    kneeArr  = [r["knee"]  for r in rows]
    ankleArr = [r["ankle"] for r in rows]

    t0   = raw_t[0]
    # t_ms tính theo giây từ lúc bắt đầu phiên đo
    t_ms = [round((t - t0) / 1000.0, 3) for t in raw_t]

    return render_template_string(
        CHARTS_HTML,
        username=current_user.id,
        t_ms=t_ms,
        hip=hipArr,
        knee=kneeArr,
        ankle=ankleArr,
        patient_code=patient_code,
        exercise_name=exercise_name,
    )

@app.route("/settings")
@login_required
def settings():
    return "<h3 style='font-family:system-ui;padding:16px'>Trang Cài đặt (đang phát triển)</h3>"


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
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hiệu chuẩn</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{ --blue:#1669c9; --sbw:260px; }

/* Nền + font giống các trang khác */
body{
  background:#e8f3ff;
  margin:0;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

/* Bố cục & sidebar giống Patients/Charts */
.layout{
  display:flex;
  gap:16px;
  position:relative;
}
.sidebar{
  background:var(--blue); color:#fff;
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
  will-change:flex-basis,max-width,transform;
}
.main-col{
  flex:1 1 auto;
  min-width:0;
}

/* Sidebar thu gọn khi bấm 3 gạch */
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

/* Nút 3 gạch trên navbar */
#btnToggleSB{
  border:2px solid #d8e6ff;
  border-radius:10px;
  background:#fff;
  padding:6px 10px;
  font-weight:700;
}
#btnToggleSB:hover{ background:#f4f8ff; }

/* Nút menu bên trái */
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
.menu-btn:hover{ background:#1f80ea; color:#fff; }
.menu-btn.active{ background:#0f5bb0; }

/* Khung video chính giữa */
.video-card{
  background:#ffffff;
  border-radius:18px;
  box-shadow:0 10px 30px rgba(15,23,42,.16);
  padding:18px 18px 22px;
  max-width:1100px;
  margin:24px auto 32px auto;  /* căn giữa */
}
.video-title{
  font-weight:700;
  color:#0a3768;
  margin-bottom:12px;
}
.video-frame{
  border-radius:16px;
  overflow:hidden;
  background:#000;
}
.video-frame video{
  width:100%;
  height:100%;
  display:block;
}
</style>
</head>
<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <a class="btn btn-outline-secondary" href="/logout">Đăng xuất</a>
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="40">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold">MENU</div>
        <a class="menu-btn" href="/">Trang chủ</a>
        <a class="menu-btn active" href="/calibration">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/patients">Xem lại</a>
        <a class="menu-btn" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="video-card">
        <div class="video-title">HƯỚNG DẪN HIỆU CHUẨN IMU</div>
        <div class="video-frame ratio ratio-16x9">
          <video autoplay loop muted controls playsinline>
            <source src="{{ url_for('static', filename='videos/calibration_loop.mp4') }}" type="video/mp4">
            Trình duyệt của bạn không hỗ trợ video.
          </video>
        </div>
      </div>
    </main>
  </div>
</div>

<script>
document.getElementById('btnToggleSB').addEventListener('click', () => {
  document.body.classList.toggle('sb-collapsed');
});
</script>
</body></html>
"""


# ======= Patients List (Xem lại) – sidebar thu gọn kiểu hiệu chuẩn =======
PATIENTS_LIST_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Danh sách bệnh nhân</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{ --blue:#1669c9; --sbw:260px; }

/* NỀN giống các trang khác */
body{ background:#e8f3ff; }

/* Layout + sidebar đồng bộ */
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

/* Mặc định thu gọn hoàn toàn */
.sb-collapsed .sidebar-col{ flex-basis:0; max-width:0; transform:translateX(-8px); }
.sb-collapsed .sidebar{ padding:0; width:0; border-radius:0; }
.sb-collapsed .sidebar *{ display:none; }

/* Navbar button */
#btnToggleSB{
  border:2px solid #d8e6ff; border-radius:10px; background:#fff;
  padding:6px 10px; font-weight:700;
}
#btnToggleSB:hover{ background:#f4f8ff; }

/* Thẩm mỹ bảng + card */
.card{ border-radius:14px; box-shadow:0 8px 18px rgba(16,24,40,.06) }
.table thead th{ background:#eef5ff; color:#0a3768 }
.search{ border-radius:10px }

/* Nút menu sidebar */
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
    <span class="navbar-brand mb-0">Danh sách bệnh nhân</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <a class="btn btn-primary px-3" href="/">Trang chủ</a>
      <img src="{{ url_for('static', filename='unnamed.png') }}" height="40">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold">MENU</div>
        <a class="menu-btn" href="/">Trang chủ</a>
        <a class="menu-btn" href="/calibration">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage">Thông tin bệnh nhân</a>
        <a class="menu-btn active" href="/patients">Xem lại</a>
        <a class="menu-btn" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="card p-3">
        <div class="row g-2 align-items-center mb-2">
          <div class="col-sm-6">
            <input id="q" class="form-control search" placeholder="Tìm kiếm... (tên, CCCD, mã)">
          </div>
          <div class="col-sm-6 text-sm-end">
          </div>
        </div>

        <div class="table-responsive">
          <table class="table table-hover align-middle" id="tbl">
            <thead>
              <tr>
                <th style="width:60px">#</th>
                <th>Mã Bệnh Nhân</th>
                <th>Họ và Tên</th>
                <th>Ngày Sinh</th>
                <th>CCCD</th>
                <th>Giới tính</th>
              </tr>
            </thead>
            <tbody>
              {% for r in rows %}
              <tr>
                <td>{{ loop.index }}</td>
                <td>{{ r.code }}</td>
                <td>{{ r.full_name }}</td>
                <td>{{ r.dob }}</td>
                <td>{{ r.national_id }}</td>
                <td>{{ r.sex }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </main>
  </div>
</div>

<script>
/* Toggle sidebar đồng bộ với các trang khác */
document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* Lọc nhanh */
const q = document.getElementById('q');
q.addEventListener('input', ()=>{
  const kw = q.value.toLowerCase();
  for (const tr of document.querySelectorAll('#tbl tbody tr')){
    const text = tr.innerText.toLowerCase();
    tr.style.display = text.includes(kw) ? "" : "none";
  }
});
</script>
</body></html>
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
      <a class="btn btn-outline-secondary" href="/patients">← Danh sách</a>
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
DASH_HTML = """ 
<!doctype html><html lang="vi"><head>
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
@media (min-width:992px){ .pull-up-guide{ margin-top: calc(-1 * var(--video-h) - 16px); } }

#btnToggleSB{ border:2px solid #d8e6ff; border-radius:10px; background:#fff; padding:6px 10px; font-weight:700; }
#btnToggleSB:hover{ background:#f4f8ff; }

.menu-btn{ width:100%; display:block; background:#1973d4; border:none; color:#fff; padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600; text-align:left; text-decoration:none; }
.menu-btn:hover{ background:#1f80ea; color:#fff }

/* nền khung three: xanh nhạt; muốn trắng đổi thành #ffffff */
#threeMount{ background:#eaf2ff; }
</style>
</head>
<body class="compact sb-collapsed">
<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <a class="btn btn-outline-secondary" href="/logout">Đăng xuất</a>
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="48">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold">MENU</div>
        <a class="menu-btn" href="/">Trang chủ</a>
        <a class="menu-btn" href="/calibration">Hiệu chuẩn</a>
        <a class="menu-btn" href="/patients/manage">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/patients">Xem lại</a>
        <a class="menu-btn" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <!-- Main -->
    <main class="main-col">
      <div class="row g-3">
        <div class="col-lg-7">
          <div class="panel mb-3">
            <div class="d-flex gap-2">
              <!-- Nút này được JS bắt sự kiện để mở modal -->
              <a class="btn btn-outline-thick flex-fill" href="#" id="btnPatientList">Danh sách bệnh nhân</a>
              <a class="btn btn-outline-thick flex-fill" href="/patients/new">Thêm bệnh nhân mới</a>
            </div>
            <div class="mt-3 d-flex align-items-center gap-3">
              <label class="form-label mb-0">Nhịp tim :</label>
              <input class="form-control" id="heartRate" style="max-width:180px">
              <span class="badge text-bg-light border">bpm</span>
            </div>
            <div class="mt-3 panel">
              <div class="table-responsive">
                <table class="table table-sm align-middle">
                  <thead><tr><th>Hip</th><th>Knee</th><th>Ankle</th></tr></thead>
                  <tbody id="tblAngles"><tr><td>--</td><td>--</td><td>--</td></tr></tbody>
                </table>
              </div>
            </div>
          </div>
        </div>

        <div class="col-lg-5">
          <div class="panel mb-3">
            <div class="row g-2">
             <div class="col-6">
               <label class="form-label">Họ và tên :</label>
               <input id="pat_name" class="form-control">
             </div>

             <div class="col-6">
               <label class="form-label">Ngày sinh :</label>
               <input id="pat_dob" type="date" class="form-control">
             </div>

             <div class="col-6">
               <label class="form-label">CCCD :</label>
               <input id="pat_cccd" class="form-control">
             </div>

             <div class="col-6">
               <label class="form-label">Giới tính :</label>
               <input id="pat_gender" class="form-control">
             </div>

             <div class="col-6">
               <label class="form-label">Cân nặng :</label>
               <input id="pat_weight" class="form-control">
             </div>

             <div class="col-6">
               <label class="form-label">Chiều cao :</label>
               <input id="pat_height" class="form-control">
             </div>

             <input type="hidden" id="pat_code">

             <!-- BÀI KIỂM TRA + NGÀY ĐO -->
             <div class="col-8">
               <label class="form-label">Bài kiểm tra :</label>
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
               <label class="form-label">Ngày đo :</label>
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
              <span class="title-chip">MÔ PHỎNG 3D</span>
              <div class="small text-muted">Nguồn: hip/knee/ankle từ IMU (độ)</div>
            </div>

            <div id="threeMount"
                 style="width:100%; height:480px; min-height:480px; border-radius:14px; overflow:visible; position:relative; z-index:1;">
            </div>

            <div class="text-center mt-2">
              <span class="badge text-bg-light border me-2">Hip: <span id="liveHip">--</span>°</span>
              <span class="badge text-bg-light border me-2">Knee: <span id="liveKnee">--</span>°</span>
              <span class="badge text-bg-light border">Ankle: <span id="liveAnkle">--</span>°</span>
            </div>

            <div class="mt-3 text-center">
               <button class="btn btn-outline-thick px-4 py-2" id="btnResetPose3D">Reset 3D</button>

               <div class="small text-muted mt-2" id="status3D">
                    Đang khởi tạo 3D…
               </div>
            </div>

          </div>
        </div>

        <!-- NÚT + KẾT QUẢ -->
        <div class="col-lg-5">
          <div class="panel d-grid gap-3">
            <button class="btn btn-outline-thick py-3" id="btnStart">Bắt đầu đo</button>
            <button class="btn btn-outline-thick py-3" id="btnStop">Kết thúc đo</button>
            <button class="btn btn-outline-thick py-3" id="btnSave">Lưu kết quả</button>

            <!-- Kết quả bài hiện tại (hiện tại không dùng nữa, để sẵn nếu sau này cần) -->
            <div id="exercise-result-panel" class="mt-3" style="display:none;">
              <h6 id="exercise-title-text" class="fw-bold mb-2"></h6>
              <div style="height:160px;">
                <canvas id="exercise-chart"></canvas>
              </div>
              <div class="mt-2 small">
                <div>ROM Hip: <span id="rom-hip-text">0°</span></div>
                <div>ROM Knee: <span id="rom-knee-text">0°</span></div>
                <div>ROM Ankle: <span id="rom-ankle-text">0°</span></div>
                <div class="mt-1 fw-bold">
                  Điểm bài này: <span id="score-text">0</span> / 2
                </div>
              </div>
              <div class="mt-3 d-flex gap-2">
                <button id="btn-next-ex" class="btn btn-outline-thick flex-grow-1">
                  Bài tập tiếp theo
                </button>
              </div>
            </div>

            <!-- Tổng kết tất cả bài (hiện tại không dùng nữa, sẽ tổng hợp ở tab Biểu đồ) -->
            <div id="all-exercise-summary" class="mt-3" style="display:none;">
              <h6 class="fw-bold">Tổng kết tất cả bài tập</h6>
              <ul class="small mb-2" id="summary-list"></ul>
              <div class="fw-bold">
                Tổng điểm: <span id="total-score-text">0</span>
              </div>
            </div>
          </div> 
        </div>

        <!-- HƯỚNG DẪN -->
        <div class="col-lg-12">
          <div class="panel">
            <div class="text-center mb-3"><span class="title-chip">HƯỚNG DẪN QUY TRÌNH ĐO</span></div>
            <div class="row g-2">
              <div class="col-md-3">
                <a class="panel d-block text-decoration-none" href="/calibration?guide=1">Bước 1: Hiệu chuẩn thiết bị</a>
              </div>
              <div class="col-md-3"><div class="panel">Bước 2: Lắp thiết bị</div></div>
              <div class="col-md-3"><div class="panel">Bước 3: Kiểm tra kết nối</div></div>
              <div class="col-md-3"><div class="panel">Bước 4: Tiến hành đo</div></div>
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
        <h5 class="modal-title">Danh sách bệnh nhân</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <input id="pm_search" class="form-control mb-2" placeholder="Tìm kiếm...">
        <div class="table-responsive" style="max-height:400px;">
          <table class="table table-hover align-middle mb-0">
            <thead>
              <tr>
                <th>#</th>
                <th>Mã</th>
                <th>Họ và tên</th>
                <th>CCCD</th>
                <th>Ngày sinh</th>
                <th>Giới tính</th>
              </tr>
            </thead>
            <tbody id="pm_body"></tbody>
          </table>
        </div>
        <div class="small text-muted mt-2">Nhấp đúp vào 1 dòng để chọn bệnh nhân.</div>
      </div>
    </div>
  </div>
</div>

<!-- Bootstrap JS (để dùng Modal) -->
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>

<script>
// ===== Video hướng dẫn & sidebar =====
const videosMap = {{ videos|tojson }};
const videoKeys = Object.keys(videosMap || {});
const sel = document.getElementById('exerciseSelect');
const vid = document.getElementById('guideVideo');

// đưa ra global để script sau dùng
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
    const exists = (window.EXERCISE_KEYS || []).some(
      k => k.toLowerCase() === key.toLowerCase()
    );
    if (exists) {
      alert('Bài tập này đã có trong danh sách.');
      return;
    }
    const opt = document.createElement('option');
    opt.value = key;
    opt.textContent = key;
    sel.appendChild(opt);
    window.EXERCISE_KEYS.push(key);
    window.videosMap[key] = null; 
    sel.value = key;
    window.currentExerciseIndex = window.EXERCISE_KEYS.length - 1;
    if (typeof window.updateVideo === 'function') {
      window.updateVideo(key);
    }
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
    vid.removeAttribute('src'); vid.load();
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

// gọi lần đầu
window.updateVideo();

// Toggle sidebar
document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* ===== Modal chọn bệnh nhân & fill form bên phải ===== */
let PAT_CACHE = null;

function fillPatientOnDashboard(rec){
  document.getElementById('pat_name').value   = rec.name || "";
  document.getElementById('pat_cccd').value   = rec.ID || "";
  document.getElementById('pat_dob').value    = rec.DateOfBirth || "";
  document.getElementById('pat_gender').value = rec.Gender || "";
  document.getElementById('pat_weight').value = rec.Weight || "";
  document.getElementById('pat_height').value = rec.Height || "";
  const code = rec.PatientCode || rec.Patientcode || "";
  const codeInput = document.getElementById('pat_code');
  if (codeInput) codeInput.value = code;
}

function renderPatRows(rows){
  const tbody = document.getElementById('pm_body');
  tbody.innerHTML = "";
  rows.forEach((r,i)=>{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${i+1}</td>
      <td>${r.code||""}</td>
      <td>${r.full_name||""}</td>
      <td>${r.national_id||""}</td>
      <td>${r.dob||""}</td>
      <td>${r.sex||""}</td>
    `;
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

// search trong modal
document.getElementById('pm_search').addEventListener('input', (e)=>{
  const kw = e.target.value.toLowerCase();
  const trs = document.querySelectorAll('#pm_body tr');
  trs.forEach(tr=>{
    tr.style.display = tr.innerText.toLowerCase().includes(kw) ? "" : "none";
  });
});
</script>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<!-- Chart.js để vẽ biểu đồ từng bài (nếu sau này dùng panel kết quả tại chỗ) -->
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<script type="module">
  import * as THREE from 'https://unpkg.com/three@0.154.0/build/three.module.js';
  window.THREE = THREE;
  import { GLTFLoader } from 'https://unpkg.com/three@0.154.0/examples/jsm/loaders/GLTFLoader.js';
  import { OrbitControls } from 'https://unpkg.com/three@0.154.0/examples/jsm/controls/OrbitControls.js';

  const mount = document.getElementById('threeMount');
  const statusEl = document.getElementById('status3D');

  // Scene
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeaf2ff);

  // Camera
  const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
  camera.position.set(0, 120, 260);

  // Renderer
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(mount.clientWidth, mount.clientHeight);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  mount.appendChild(renderer.domElement);
  renderer.domElement.style.width  = "100%";
  renderer.domElement.style.height = "100%";
  renderer.domElement.style.display = "block";

  // Lights
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.3));
  const dir = new THREE.DirectionalLight(0xffffff, 1.1);
  dir.position.set(2, 4, 2);
  scene.add(dir);

  // Controls
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.enablePan = false;
  controls.enableRotate = false;

  // Grid
  const GRID_SIZE = 240;
  const grid = new THREE.GridHelper(GRID_SIZE, 24, 0x999999, 0xcccccc);
  grid.position.y = 0;
  scene.add(grid);

  // Resize
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

  // Pivot
  const legPivot = new THREE.Group();
  legPivot.position.set(0, 0, 0);
  scene.add(legPivot);
  window.legPivot = legPivot;

  // Load GLB
  const loader = new GLTFLoader();
  const GLB_URL = "{{ url_for('static', filename='leg_model.glb') }}";

  loader.load(
    GLB_URL,
    (gltf) => {
      const model = gltf.scene || gltf.scenes?.[0];
      if (!model) { statusEl.textContent = "⚠️ GLB không có scene."; return; }

      // Ẩn mesh tĩnh, chỉ giữ SkinnedMesh
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

      // Chuẩn hoá pose rồi bind lại
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

      // Fit vào khung & đặt chạm sàn
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

      // Camera side-view
      const sphere = new THREE.Sphere(); new THREE.Box3().setFromObject(model).getBoundingSphere(sphere);
      const sideDist = sphere.radius * 2.2;
      camera.position.set(sideDist, sphere.radius * 0.35, 0);
      camera.lookAt(0, sphere.center.y, 0);
      controls.target.set(0, sphere.center.y, 0);
      controls.update();
      controls.minDistance = sphere.radius * 0.8;
      controls.maxDistance = sphere.radius * 3.0;

      /* ====== ĐA-SKELETON: gom mọi bone trùng tên ====== */
      const BONE_REG = new Map(); // name(lowercase) -> array of Bone
      for (const sm of window.SKINS) {
        for (const b of sm.skeleton.bones) {
          const key = (b.name || '').toLowerCase();
          if (!key) continue;
          if (!BONE_REG.has(key)) BONE_REG.set(key, []);
          BONE_REG.get(key).push(b);
          if (!b.userData.bindQ) b.userData.bindQ = b.quaternion.clone();
        }
      }

      const NAME_MAP = {
        hip:   'thighL',
        knee:  'shinL',
        ankle: 'footL'
      };

      function getBones(joint) {
        const key = (NAME_MAP[joint] || '').toLowerCase();
        return BONE_REG.get(key) || [];
      }

      const AXISVEC = { x:new THREE.Vector3(1,0,0), y:new THREE.Vector3(0,1,0), z:new THREE.Vector3(0,0,1) };
      const AXIS =  { hip:'x', knee:'x', ankle:'x' };
      const SIGN =  { hip: -1,  knee: 1,  ankle: 1  };
      const OFF  =  { hip: 0,  knee: 0,  ankle: -90  };
      const toRad = d => (Number(d)||0) * Math.PI/180;

      window.setAxis   = (joint, axis, sign=1)=>{ AXIS[joint]=axis; SIGN[joint]=Math.sign(sign)||1; };
      window.setOffset = (joint, deg)=>{ OFF[joint]=Number(deg)||0; };
      window.dumpBones = ()=> Array.from(BONE_REG.keys());

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
        setJointDeg('hip',   hip);
        setJointDeg('knee',  knee);
        setJointDeg('ankle', ankle_real);
      };

      window.legReady = true;
      if (window._pendingAngles) {
        const a = window._pendingAngles; window._pendingAngles = null;
        window.applyLegAngles(a.hip, a.knee, a.ankle);
      }

      // Reset 3D
      document.getElementById('btnResetPose3D')?.addEventListener('click', () => {
        for (const arr of BONE_REG.values())
          for (const b of arr) if (b.userData.bindQ) b.quaternion.copy(b.userData.bindQ);
      });

      const bbox = new THREE.Box3().setFromObject(model);
      const size = bbox.getSize(new THREE.Vector3());
      const rad  = size.length() * 0.5 || 1;
      camera.near = Math.max(0.1, rad * 0.01);
      camera.far  = rad * 20;
      camera.updateProjectionMatrix();

      statusEl.textContent = "✅ Mô hình đã sẵn sàng";
    },
    (progress) => {
      const percent = (progress.loaded / (progress.total || 1)) * 100;
      statusEl.textContent = `Đang tải mô hình: ${percent.toFixed(0)}%`;
    },
    (err) => {
      console.error("❌ Lỗi load GLB:", err);
      statusEl.textContent = "❌ Không tải được mô hình 3D.";
    }
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

<!-- Socket & Start/Stop -->
<script id="imu-handlers">
  const btnSave   = document.getElementById("btnSave");
  const btnStart  = document.getElementById("btnStart");
  const btnStop   = document.getElementById("btnStop");
  const exerciseSelect = document.getElementById("exerciseSelect");
  const resultPanel  = document.getElementById("exercise-result-panel");
  const summaryPanel = document.getElementById("all-exercise-summary");
  const btnNextEx    = document.getElementById("btn-next-ex");

  if (btnStop) btnStop.disabled = true;
   // ===== GIẢ LẬP NHỊP TIM – chỉ chạy khi đang đo =====
  let heartSimTimer = null;
  let heartVal = 75;
  let heartDir = 1;

  function startHeartSim(){
    const el = document.getElementById("heartRate");
    if (!el) return;
    if (heartSimTimer) return;   // đang chạy rồi

    const MIN = 70;
    const MAX = 95;

    function step(){
      // nếu đã dừng đo thì dừng luôn giả lập
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

    // reset giá trị mỗi lần bắt đầu
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
  let currentSamples = [];   // {hip,knee,ankle}
  let exerciseResults = {};  // name -> {romHip,romKnee,romAnkle,score,samples}
  let exerciseChart = null;

  function getCurrentExerciseName(){
    return exerciseSelect ? (exerciseSelect.value || "exercise") : "exercise";
  }

  function getExerciseIndex(name){
    const idx = EXERCISE_ORDER.indexOf(name);
    return idx >= 0 ? idx : 0;
  }

  // (Các hàm showExerciseResult, showAllSummary, btnNextEx hiện không dùng – để lại cho tương lai)

  // ========== NÚT LƯU THÔNG TIN BỆNH NHÂN ==========
  if (btnSave) btnSave.addEventListener("click", async () => {
    const name   = document.getElementById('pat_name').value.trim();
    const cccd   = document.getElementById('pat_cccd').value.trim();
    const dob    = document.getElementById('pat_dob').value.trim();
    const gender = document.getElementById('pat_gender').value.trim();
    const weight = document.getElementById('pat_weight').value.trim();
    const height = document.getElementById('pat_height').value.trim();
    const codeEl = document.getElementById('pat_code');
    const patient_code = codeEl ? (codeEl.value || "").trim() : "";

    if (!name){
      alert("Vui lòng nhập HỌ VÀ TÊN bệnh nhân trước khi lưu.");
      return;
    }

    const payload = {
      patient_code: patient_code,
      name:         name,
      national_id:  cccd,
      dob:          dob,
      gender:       gender,
      weight:       weight,
      height:       height
    };

    try {
      const res = await fetch("/api/patients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const j = await res.json();

      if (!j.ok) {
        alert(j.msg || "Lưu thông tin bệnh nhân thất bại.");
        return;
      }

      if (codeEl && j.patient_code) {
        codeEl.value = j.patient_code;
      }

      alert("Đã lưu thông tin bệnh nhân: " + (j.patient_code || patient_code || "(mới)"));
    } catch (e) {
      console.error(e);
      alert("Có lỗi khi gửi dữ liệu lên server.");
    }
  });

  // ========== SOCKET IO – cập nhật góc & thu mẫu ==========
  window.socket = window.socket || io({
    transports: ['websocket'],
    upgrade: false,
    reconnection: true,
    reconnectionAttempts: 10,
    reconnectionDelay: 500
  });
  const socket = window.socket;

  socket.on('connect', () => console.log('[SOCKET] connected:', socket.id));
  socket.on('connect_error', (err) => console.error('[SOCKET] connect_error:', err));
  socket.on('disconnect', (r) => console.warn('[SOCKET] disconnected:', r));

  socket.on("imu_data", (msg) => {
    // Bảng số trực tiếp
    const tr = document.querySelector("#tblAngles tr");
    if (tr) {
      const tds = tr.querySelectorAll("td");
      if (tds.length >= 3) {
        if (msg.hip   != null) tds[0].textContent = Number(msg.hip).toFixed(2);
        if (msg.knee  != null) tds[1].textContent = Number(msg.knee).toFixed(2);
        if (msg.ankle != null) tds[2].textContent = Number(msg.ankle).toFixed(2);
      }
    }

    // Badge nhỏ dưới 3D
    if (msg.hip   != null) document.getElementById('liveHip').textContent   = Number(msg.hip).toFixed(1);
    if (msg.knee  != null) document.getElementById('liveKnee').textContent  = Number(msg.knee).toFixed(1);
    if (msg.ankle != null) document.getElementById('liveAnkle').textContent = Number(msg.ankle).toFixed(1);

    // Nếu đang đo thì lưu mẫu để vẽ biểu đồ & tính ROM
    if (isMeasuring){
      const hip   = Number(msg.hip   ?? 0);
      const knee  = Number(msg.knee  ?? 0);
      const ankle = Number(msg.ankle ?? 0);
      currentSamples.push({hip,knee,ankle});
    }

    // 3D
    const hip   = msg.hip   ?? 0;
    const knee  = msg.knee  ?? 0;
    const ankle = msg.ankle ?? 0;
    if (typeof window.pushAngles === "function") {
      window.pushAngles(hip, knee, ankle);
    } else {
      window._pendingAngles = { hip, knee, ankle };
    }
  });

  // ========== NÚT BẮT ĐẦU / KẾT THÚC ĐO ==========
  if (btnStart) btnStart.addEventListener("click", async () => {
    if (isMeasuring) return;
    try {
      const curName   = getCurrentExerciseName();
      const firstName = EXERCISE_ORDER[0];
      if (curName === firstName) {
        localStorage.removeItem("exerciseScores");
      }
    } catch(e){}
    const r = await fetch("/session/start", { method: "POST" });
    const j = await r.json();
    console.log("[START RESPONSE]", j);
    if (!j.ok) {
      alert(j.msg || "Không start được phiên đo");
      return;
    }

    isMeasuring = true;
    currentSamples = [];
    startHeartSim();

    btnStart.disabled  = true;
    btnStart.textContent = "Đang đo...";
    btnStop.disabled   = false;
    btnStop.textContent  = "Kết thúc đo";
    resultPanel.style.display  = "none";
    summaryPanel.style.display = "none";
  });

  if (btnStop) btnStop.addEventListener("click", async () => {
     const r = await fetch("/session/stop", { method: "POST" });
     let j = {};
     try { j = await r.json(); } catch(e){}

     isMeasuring = false;
     stopHeartSim();
     btnStart.disabled = false;
     btnStop.disabled  = true;
     btnStart.textContent = "Bắt đầu đo";

     // Tính ROM & Score từ currentSamples
     let romHip = 0, romKnee = 0, romAnkle = 0, score = 0;
     let maxKnee = 0, minKnee = 0;

     if (currentSamples.length){
        const hips   = currentSamples.map(s => s.hip);
        const knees  = currentSamples.map(s => s.knee);
        const ankles = currentSamples.map(s => s.ankle);

        const maxHip   = Math.max(...hips);
        const minHip   = Math.min(...hips);
        maxKnee        = Math.max(...knees);
        minKnee        = Math.min(...knees);
        const maxAnkle = Math.max(...ankles);
        const minAnkle = Math.min(...ankles);

        romHip   = maxHip   - minHip;
        romKnee  = maxKnee  - minKnee;
        romAnkle = maxAnkle - minAnkle;
        score    = fmaScore(romKnee);
     }

     const exName = getCurrentExerciseName();
     const result = {
        name: exName,
        romHip, romKnee, romAnkle,
        score,
        maxKnee,
        minKnee
     };

     // Lưu vào localStorage (để tab Biểu đồ đọc lại)
     let store = {};
     try { store = JSON.parse(localStorage.getItem("exerciseScores") || "{}"); }
     catch(e){ store = {}; }
     store[exName] = result;
     localStorage.setItem("exerciseScores", JSON.stringify(store));

     // Lấy patient code
     const pat = (document.getElementById("pat_code")?.value || "").trim();

     // Redirect sang trang CHARTS_HTML để xem biểu đồ & đánh giá
     let url = "/charts?exercise=" + encodeURIComponent(exName);
     if (pat) url += "&patient_code=" + encodeURIComponent(pat);

     window.location.href = url;
  });

  // Tự động chọn bài tập khi quay lại từ /charts?next_ex=...
  const urlParams = new URLSearchParams(window.location.search);

  if (urlParams.has("next_ex")) {
      const nextEx = urlParams.get("next_ex").trim();

      const sel = document.getElementById("exerciseSelect");
      if (sel) {
          const options = [...sel.options].map(o => o.value.toLowerCase());
          const foundIndex = options.indexOf(nextEx.toLowerCase());

          if (foundIndex >= 0) {
              sel.value = sel.options[foundIndex].value;
          } else {
              const opt = document.createElement("option");
              opt.value = nextEx;
              opt.textContent = nextEx;
              sel.appendChild(opt);
              sel.value = nextEx;
          }
      }

      if (typeof window.updateVideo === "function") {
          window.updateVideo(nextEx);
      }

      if (window.EXERCISE_KEYS) {
          const idx = window.EXERCISE_KEYS.indexOf(nextEx);
          if (idx >= 0) window.currentExerciseIndex = idx;
      }
  }
</script> 
</script>

</body></html>
"""



CHARTS_HTML = """
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
.menu-btn:hover{ background:#1f80ea; }
.menu-btn.active{ background:#0f5bb0; }

.panel{
  background:#fff;
  border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,0.10);
  padding:16px;
  margin-bottom:16px;
}

.chart-box{ height:260px; }

/* Khối đánh giá */
.eval-panel{
  background:#ffffff;
  border-radius:18px;
  box-shadow:0 10px 24px rgba(15,23,42,.16);
  padding:18px 18px 14px 18px;
}
.eval-header{
  font-weight:800;
  color:#0b3769;
  font-size:1.1rem;
}
.eval-subtitle{
  font-size:.9rem;
  color:#64748b;
}
.eval-item{
  font-size:.95rem;
}
.eval-item + .eval-item{
  border-top:1px dashed #e2e8f0;
  margin-top:10px;
  padding-top:10px;
}

.eval-badge{
  font-size:.8rem;
  padding:4px 8px;
  border-radius:999px;
}

#totalScore{
  font-size:.95rem;
  padding:6px 10px;
  border-radius:999px;
}

/* nhấn mạnh nhãn đánh giá (Yếu / Trung bình / Tốt) */
.strength-label{
  font-weight:700;
  font-size:1rem;
  color:#0b3769;
}
.strength-desc{
  font-size:.9rem;
  color:#6b7280;
}

/* NOTE BOX cho mô tả đánh giá */
.strength-desc{
  font-size:.9rem;
  color:#0b3769;   /* MÀU XANH ĐẬM CHO RÕ */
  font-weight:500;
  background:#e8f5ff;

  border-radius:10px;
}


/* Tổng điểm các bài đã đo – to, ở giữa */
.total-summary{
  margin-top:10px;
  text-align:center;
  font-weight:800;
  font-size:1.05rem;
  color:#0b3769;
}
.total-summary span{
  display:inline-block;
  margin-left:6px;
  padding:4px 14px;
  border-radius:999px;
  background:#1d4ed8;
  color:#fff;
  font-size:1rem;
}
</style>
</head>

<body class="sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>

    <div class="ms-auto d-flex align-items-center gap-3">
      <a class="btn btn-outline-secondary" href="/logout">Đăng xuất</a>
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
        <a class="menu-btn" href="/patients">Xem lại</a>
        <a class="menu-btn active" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <main class="main-col">
      <div class="row g-3">

        <div class="col-lg-9">
          <div class="panel">
            <div class="d-flex justify-content-between align-items-center">

              <div>
                <h5 class="mb-1">Biểu đồ góc khớp theo thời gian</h5>
                <div class="text-muted small">Phiên đo gần nhất.</div>

                {% if exercise_name %}
                <div class="text-muted small">Bài tập: <strong>{{ exercise_name }}</strong></div>
                {% endif %}

                {% if patient_code %}
                <div class="text-muted small">Mã bệnh nhân: <strong>{{ patient_code }}</strong></div>
                {% endif %}
              </div>

              <div class="d-flex gap-2">
                <a class="btn btn-outline-success btn-sm"
                   href="/session/export_csv{% if patient_code %}?patient_code={{ patient_code }}{% endif %}"
                   target="_blank">
                  Lưu CSV
                </a>

                <a class="btn btn-outline-primary btn-sm" href="/charts_emg">EMG</a>

                <button id="btnNextEx" class="btn btn-primary btn-sm">
                  Bài tập tiếp theo
                </button>
              </div>

            </div>
          </div>

          <div class="panel"><h6>Hip (độ)</h6><div class="chart-box"><canvas id="hipChart"></canvas></div></div>
          <div class="panel"><h6>Knee (độ)</h6><div class="chart-box"><canvas id="kneeChart"></canvas></div></div>
          <div class="panel"><h6>Ankle (độ)</h6><div class="chart-box"><canvas id="ankleChart"></canvas></div></div>
        </div>

        <div class="col-lg-3">
          <div class="eval-panel mb-3">
            <div class="eval-header mb-1">Đánh giá FMA</div>

            <div id="evalContent">
              <div class="d-flex align-items-center justify-content-center py-4">
                <div class="spinner-border text-primary me-2"></div>
                <span class="small text-muted">Đang xử lý...</span>
              </div>
            </div>

            <hr class="my-2">

            <div id="totalBox" class="small mb-2">
              <span class="me-1 fw-semibold">Điểm bài hiện tại:</span>
              <span id="totalScore" class="badge bg-primary ms-1">0 / 2</span>
            </div>

            <hr class="my-2">
            <div class="small fw-bold mb-1">Tổng kết các bài đã đo</div>
            <div id="allExercisesSummary" class="small"></div>

          </div>

          <!-- Bảng EMG -->
          <div class="panel">
            <div class="eval-header mb-1">Tín hiệu điện cơ EMG</div>
            <table class="table table-sm mb-0">
              <tbody>
                <tr>
                  <th scope="row">Cơ đùi</th>
                  <td class="text-end">
                    <span style="
                        background:#dcfce7;
                        color:#166534;
                        padding:4px 10px;
                        border-radius:8px;
                        font-weight:600;
                        font-size:0.85rem;
                    ">Khỏe</span>
                  </td>
                </tr>
                <tr>
                  <th scope="row">Cơ cẳng chân</th>
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
document.getElementById("btnToggleSB").onclick = () =>
  document.body.classList.toggle("sb-collapsed");

// Dữ liệu từ server
// Dữ liệu từ server (thô)
const t_ms_raw    = {{ t_ms|tojson }};
const hip_raw     = {{ hip|tojson }};
const knee_raw    = {{ knee|tojson }};
const ankle_raw   = {{ ankle|tojson }};
const currentExerciseName = {{ (exercise_name or '')|tojson }};
const patientCode         = {{ (patient_code or '')|tojson }};

// ===== CHỈ LẤY 5 GIÂY CUỐI (có thể đổi WINDOW_MS = 3000 cho 3s) =====
const WINDOW_MS = 6000;

let t_ms    = t_ms_raw;
let hipArr  = hip_raw;
let kneeArr = knee_raw;
let ankleArr= ankle_raw;

if (t_ms_raw && t_ms_raw.length) {
  const lastT = t_ms_raw[t_ms_raw.length - 1];
  const minT  = lastT - WINDOW_MS;

  // tìm index đầu tiên >= minT
  let startIdx = 0;
  while (startIdx < t_ms_raw.length && t_ms_raw[startIdx] < minT) {
    startIdx++;
  }

  // nếu khoảng đo dài hơn WINDOW_MS thì cắt
  if (startIdx > 0 && startIdx < t_ms_raw.length) {
    t_ms     = t_ms_raw.slice(startIdx);
    hipArr   = hip_raw.slice(startIdx);
    kneeArr  = knee_raw.slice(startIdx);
    ankleArr = ankle_raw.slice(startIdx);
  }
}



const evalBox = document.getElementById("evalContent");
const totalScoreSpan = document.getElementById("totalScore");

const commonOptions = {
  responsive:true, maintainAspectRatio:false,
  interaction:{ mode:"index", intersect:false },
  plugins:{ legend:{ display:false }},
  scales:{
    x:{ title:{ display:true, text:"t (ms)" }},
    y:{ title:{ display:true, text:"Góc (°)" }, min:0, max:120 }
  }
};

function makeChart(id, arr){
  new Chart(document.getElementById(id), {
    type:"line",
    data:{ labels:t_ms, datasets:[{data:arr, borderWidth:2, tension:0.15 }]},
    options:commonOptions
  });
}

makeChart("hipChart", hipArr);
makeChart("kneeChart", kneeArr);
makeChart("ankleChart", ankleArr);

// Quy tắc FMA (demo)
function fmaScore(rom){
  if (rom >= 90) return 2;
  if (rom >= 40 && rom<=50) return 1;
  return 0;
}

// Chuyển điểm FMA -> nhận xét cơ gối
function strengthInfo(score){
  score = Number(score) || 0;
  if (score >= 2){
    return {
      label: "Tốt",
      desc:  "Biên độ vận động lớn, kiểm soát động tác tốt.",
      badgeClass: "bg-success"
    };
  }
  if (score === 1){
    return {
      label: "Trung bình",
      desc:  "Biên độ vận động ở mức chấp nhận được, nên tiếp tục tập để cải thiện.",
      badgeClass: "bg-warning text-dark"
    };
  }
  return {
    label: "Yếu",
      desc:  "Biên độ vận động còn hạn chế, cần tăng cường tập luyện và theo dõi.",
      badgeClass: "bg-danger"
  };
}

// ====== LẤY ĐIỂM ĐÃ LƯU TỪ LOCALSTORAGE ======
let storedScores = {};
try {
  storedScores = JSON.parse(localStorage.getItem("exerciseScores") || "{}");
} catch(e) {
  storedScores = {};
}

// Thứ tự chuẩn các bài
const defaultOrder = ["ankle flexion","knee flexion","hip flexion"];
// exerciseOrder: gộp default + các key đã lưu, bỏ trùng
const exerciseOrder = Array.from(new Set([...defaultOrder, ...Object.keys(storedScores)]));

function showCurrentExerciseScore(){
  if (!currentExerciseName){
    evalBox.innerHTML = "<div class='text-muted'>Chưa có tên bài tập.</div>";
    totalScoreSpan.textContent = "0 / 2";
    return;
  }

  const data = storedScores[currentExerciseName];

  // Nếu không có trong localStorage → tính trực tiếp từ kneeArr của phiên hiện tại
  if (!data){
    if (!kneeArr.length){
      evalBox.innerHTML = "<div class='text-muted'>Không có dữ liệu ROM cho bài hiện tại.</div>";
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
      </div>
    `;

    totalScoreSpan.textContent = `${score} / 2`;
    totalScoreSpan.className = "badge ms-1 " + info.badgeClass;
    return;
  }

  // Có dữ liệu lưu trong localStorage
  const romKnee = Number(data.romKnee || 0);

  let maxK, minK;
  if (typeof data.maxKnee === "number" && typeof data.minKnee === "number") {
    maxK = data.maxKnee;
    minK = data.minKnee;
  } else if (kneeArr.length) {
    maxK = Math.max(...kneeArr);
    minK = Math.min(...kneeArr);
  } else {
    maxK = romKnee;
    minK = 0;
  }

  const info = strengthInfo(data.score);

  evalBox.innerHTML = `
    <div class='eval-item'>
      <div class='strength-label mb-1'>${info.label}</div>
      <div class="fma-note-box p-3 my-2">
        <div class='strength-desc mb-0'>${info.desc}</div>
      </div>
    </div>
  `;

  totalScoreSpan.textContent = `${data.score} / 2`;
  totalScoreSpan.className = "badge ms-1 " + info.badgeClass;
}

// ====== TỔNG KẾT TẤT CẢ BÀI ĐÃ ĐO (hiện ở dưới) ======
const allSummaryDiv = document.getElementById("allExercisesSummary");

function renderAllExercisesSummary(){
  if (!allSummaryDiv) return;

  const keys = Object.keys(storedScores);
  if (!keys.length){
    allSummaryDiv.innerHTML = "<div class='text-muted'>Chưa có bài nào được lưu.</div>";
    return;
  }

  let html = "";
  let total = 0;

  // Sắp theo thứ tự defaultOrder nếu có, rồi tới các bài khác
  const sortedNames = [...keys].sort((a, b) => {
    const ia = defaultOrder.indexOf(a);
    const ib = defaultOrder.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });

  sortedNames.forEach((name, idx) => {
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
              ROM Knee: ${(d.romKnee || 0).toFixed(1)}°
              – <span class='strength-label'>${info.label}</span>
            </div>
          </div>
          <span class='eval-badge badge ${info.badgeClass}'>${d.score} / 2</span>
        </div>
      </div>
    `;
  });

  html += `
    <div class='total-summary'>
      Tổng điểm các bài đã đo:
      <span>${total} / ${sortedNames.length * 2}</span>
    </div>
  `;

  allSummaryDiv.innerHTML = html;
}

showCurrentExerciseScore();
renderAllExercisesSummary();

// ====== NÚT "TIẾP THEO" → CHUYỂN SANG BÀI TIẾP THEO ĐỂ ĐO ======
const btnNext = document.getElementById("btnNextEx");

btnNext.onclick = () => {
  const idx = exerciseOrder.indexOf(currentExerciseName);

  // Nếu còn bài sau -> sang trang chủ với next_ex
  if (idx >= 0 && idx < exerciseOrder.length - 1){
    const nextName = exerciseOrder[idx + 1];

    let url = "/?next_ex=" + encodeURIComponent(nextName);
    if (patientCode) {
      url += "&patient_code=" + encodeURIComponent(patientCode);
    }

    window.location.href = url;
    return;
  }

  // Đã là bài cuối cùng
  let url = "/";
  if (patientCode) {
    url += "?patient_code=" + encodeURIComponent(patientCode);
  }
  alert("Đã hoàn thành các bài tập. Hệ thống sẽ quay lại trang đo.");
  window.location.href = url;
};
</script>

</body>
</html>
"""




EMG_CHART_HTML = """<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Biểu đồ EMG</title>

<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>

<style>
:root { --blue:#1669c9; --sbw:260px; }

body{
  background:#e8f3ff;
  margin:0;
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
#btnToggleSB:hover{
  background:#eef6ff;
}

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
.menu-btn:hover{ background:#1f80ea; }
.menu-btn.active{ background:#0f5bb0; }

.panel{
  background:#fff;
  border-radius:16px;
  box-shadow:0 8px 20px rgba(16,24,40,0.10);
  padding:16px;
  margin-bottom:16px;
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
      <a class="btn btn-outline-secondary" href="/logout">Đăng xuất</a>
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
        <a class="menu-btn" href="/patients">Xem lại</a>
        <a class="menu-btn" href="/charts">Biểu đồ góc</a>
        <a class="menu-btn active" href="/charts_emg">Biểu đồ EMG</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
      </div>
    </aside>

    <main class="main-col">
      <div class="panel">
        <div class="d-flex justify-content-between align-items-center">
          <div>
            <h5>Biểu đồ tín hiệu EMG</h5>
            <div class="text-muted small">
              Biên độ EMG theo thời gian (mV). Dùng cùng thời gian với phiên đo gần nhất.
            </div>
          </div>
          <a class="btn btn-outline-primary btn-sm" href="/charts">← Biểu đồ góc khớp</a>
        </div>
      </div>

      <div class="panel">
        <div class="chart-box">
          <canvas id="emgChart"></canvas>
        </div>
      </div>
    </main>
  </div>
</div>

<script>
document.getElementById("btnToggleSB").onclick = () =>
  document.body.classList.toggle("sb-collapsed");

const t_ms  = {{ t_ms|tojson }};
const emg   = {{ emg|tojson }};

const options = {
  responsive:true, maintainAspectRatio:false,
  interaction:{ mode:"index", intersect:false },
  plugins:{ legend:{ display:false }},
  scales:{
    x:{ title:{ display:true, text:"t (ms)" }},
    y:{ title:{ display:true, text:"Biên độ EMG (mV)" } }
  }
};

new Chart(document.getElementById("emgChart"), {
  type:"line",
  data:{
    labels:t_ms,
    datasets:[{ data:emg, borderColor:"#1973d4", tension:0.15 }]
  },
  options
});
</script>

</body>
</html>
"""

# ===================== Patients Manage =====================
# ======= Patients Manage (sidebar thu gọn kiểu hiệu chuẩn) =======
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
    <span class="navbar-brand mb-0">Thông tin bệnh nhân</span>
    <div class="ms-auto d-flex align-items-center gap-2">
      <a class="btn btn-outline-secondary" href="/logout">Đăng xuất</a>
      <img src="{{ url_for('static', filename='unnamed.png') }}" alt="Logo" height="40">
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
  <div class="layout">
    <!-- Sidebar -->
    <aside class="sidebar-col">
      <div class="sidebar">
        <div class="mb-2 fw-bold">MENU</div>
        <a class="menu-btn" href="/">Trang chủ</a>
        <a class="menu-btn" href="/calibration">Hiệu chuẩn</a>
        <a class="menu-btn active" href="/patients/manage">Thông tin bệnh nhân</a>
        <a class="menu-btn" href="/patients">Xem lại</a>
        <a class="menu-btn" href="/charts">Biểu đồ</a>
        <a class="menu-btn" href="/settings">Cài đặt</a>
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
                <label class="form-label">Họ và tên</label>
                <input id="name" class="form-control input-sm">
              </div>
              <div class="col-12">
                <label class="form-label">CCCD</label>
                <input id="national_id" class="form-control input-sm">
              </div>
              <div class="col-6">
                <label class="form-label">Ngày sinh</label>
                <input id="dob" class="form-control input-sm" placeholder="vd 30/05/2001 hoặc 2001-05-30">
              </div>
              <div class="col-6">
                <label class="form-label">Giới tính</label>
                <select id="gender" class="form-select input-sm">
                  <option value="">--</option>
                  <option>Male</option>
                  <option>Female</option>
                </select>
              </div>
              <div class="col-6">
                <label class="form-label">Chiều cao (cm)</label>
                <input id="height" class="form-control input-sm">
              </div>
              <input type="hidden" id="pat_code">
              <div class="col-6">
                <label class="form-label">Cân nặng (kg)</label>
                <input id="weight" class="form-control input-sm">
              </div>

              <div class="col-12">
                <label class="form-label">Mã bệnh nhân</label>
                <input id="patient_code" class="form-control input-sm" placeholder="(để trống để tạo mới)">
              </div>

              <div class="col-12 d-flex justify-content-center gap-4 mt-2">
                <button id="btnSave" class="btn btn-outline-thick py-2 px-5 fs-5">Lưu</button>
                <button id="btnDelete" class="btn btn-outline-thick py-2 px-5 fs-5">Xóa</button>
              </div>
            </div>
          </div>

          <div class="card p-3 mt-3">
            <button id="btnClearAll" class="btn btn-outline-danger w-100">Xóa toàn bộ danh sách</button>
          </div>
        </div>

        <!-- Bảng phải -->
        <div class="col-lg-7">
          <div class="card p-3">
            <input id="q" class="form-control mb-3" placeholder="Tìm kiếm...">
            <div class="table-responsive">
              <table class="table table-hover align-middle" id="tbl">
                <thead>
                  <tr>
                    <th style="width:60px">#</th>
                    <th>Mã bệnh nhân</th>
                    <th>Họ và tên</th>
                    <th>CCCD</th>
                    <th>Ngày sinh</th>
                    <th>Giới tính</th>
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



