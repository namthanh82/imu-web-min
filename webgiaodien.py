# ==================== webgiaodien.py ====================
import os, json, time, csv, threading
from datetime import datetime
from collections import defaultdict

# =========================================================
# SERIAL CONFIG
# =========================================================
SERIAL_ENABLED = os.environ.get("ENABLE_SERIAL", "0") == "1"
try:
    if SERIAL_ENABLED:
        import serial, serial.tools.list_ports
    else:
        serial = None
except Exception:
    serial = None
    SERIAL_ENABLED = False

ser = None
serial_thread = None
stop_serial_thread = False
data_buffer = []

# =========================================================
# APPEND SAMPLES (Emit SocketIO)
# =========================================================
def append_samples(samples):
    """
    samples: list of dict như {"t_ms":..., "hip":..., "knee":..., "ankle":...}
    """
    global data_buffer
    for s in samples:
        data_buffer.append(s)
        socketio.emit("imu_data", {
            "t": s.get("t_ms"),
            "hip": s.get("hip"),
            "knee": s.get("knee"),
            "ankle": s.get("ankle"),
        })

# =========================================================
# START / STOP SERIAL READER
# =========================================================
def start_serial_reader(port="COM6", baud=115200):
    """Đọc dữ liệu serial: id,timestamp,yaw,roll,pitch"""
    global ser, serial_thread, stop_serial_thread

    if not SERIAL_ENABLED:
        print("SERIAL_DISABLED – bỏ qua đọc cổng COM")
        return True

    try:
        ser = serial.Serial(port, baud, timeout=0.5)
    except Exception as e:
        print("Không mở được cổng serial:", e)
        return False

    stop_serial_thread = False
    last_angles = defaultdict(lambda: {"yaw":0,"roll":0,"pitch":0,"ts":0})

    def reader_loop():
        print(f"Đang đọc dữ liệu từ {port} ...")
        while not stop_serial_thread:
            try:
                line = ser.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 5:
                    continue
                sid  = int(parts[0])
                ts   = float(parts[1])
                yaw  = float(parts[2])
                roll = float(parts[3])
                pitch= float(parts[4])

                last_angles[sid] = {"yaw":yaw,"roll":roll,"pitch":pitch,"ts":ts}

                # Khi có đủ 3 cảm biến → gửi realtime
                if all(k in last_angles for k in (1,2,3)):
                    hip   = last_angles[1]["pitch"]
                    knee  = last_angles[2]["pitch"]
                    ankle = last_angles[3]["pitch"]
                    append_samples([{
                        "t_ms": ts, "hip": hip, "knee": knee, "ankle": ankle
                    }])

            except Exception as e:
                print("Serial read error:", e)

        print(" Dừng đọc serial")

    serial_thread = threading.Thread(target=reader_loop, daemon=True)
    serial_thread.start()
    return True


def stop_serial_reader():
    global stop_serial_thread, ser
    stop_serial_thread = True
    if ser:
        try:
            ser.close()
        except:
            pass

# =========================================================
# FLASK + SOCKETIO SETUP
# =========================================================
from flask import Flask, render_template_string, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO

# ---- Firebase (tùy chọn) ----
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
    raise FileNotFoundError("firebase-key.json not found.")

CRED_PATH = find_firebase_key()
cred = credentials.Certificate(CRED_PATH)
firebase_admin.initialize_app(cred)
fs_client = firestore.client()

# =========================================================
# APP CONFIG
# =========================================================
app = Flask(__name__)
app.secret_key = "CHANGE_ME"
PATIENTS_FILE = "sample.json"

socketio = SocketIO(app, cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = "login"

USERS = {"komlab": generate_password_hash("123456")}  # đổi khi deploy

EXERCISE_VIDEOS = {
    "ankle flexion": "/static/videos/ankle flexion.mp4",
    "hip flexion":   "/static/videos/hip flexion.mp4",
    "knee flexion":  "/static/videos/knee flexion.mp4",
}

class User(UserMixin):
    def __init__(self, u): self.id = u

@login_manager.user_loader
def load_user(u): return User(u) if u in USERS else None

# =========================================================
# PATIENT HELPERS
# =========================================================
def _ensure_patients_file():
    if not os.path.exists(PATIENTS_FILE):
        with open(PATIENTS_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

def load_patients_rows():
    _ensure_patients_file()
    with open(PATIENTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict): data = {}
    rows = []
    for code, rec in data.items():
        rows.append({
            "code": code,
            "full_name": rec.get("name", ""),
            "dob": rec.get("DateOfBirth", ""),
            "national_id": rec.get("ID", ""),
            "sex": rec.get("Gender", ""),
        })
    return sorted(rows, key=lambda r: (r["full_name"] or "").lower()), data

def gen_patient_code(full_name: str) -> str:
    last = (full_name.split()[-1] if full_name else "BN")
    base = "".join(ch for ch in last if ch.isalnum())
    suffix = datetime.now().strftime("%m%d%H%M")
    return f"{base}{suffix}"

# =========================================================
# ROUTES
# =========================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        if u in USERS and check_password_hash(USERS[u], p):
            login_user(User(u))
            return redirect(url_for("dashboard"))
        flash("Sai tài khoản hoặc mật khẩu", "danger")
    return render_template_string(LOGIN_HTML)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def dashboard():
    return render_template_string(DASH_HTML, username=current_user.id, videos=EXERCISE_VIDEOS)

# =========================================================
# START / STOP SESSION
# =========================================================
@app.post("/session/start")
@login_required
def session_start():
    global data_buffer
    data_buffer = []

    if SERIAL_ENABLED:
        ok = start_serial_reader(port=os.environ.get("SERIAL_PORT", "COM6"), baud=115200)
        if not ok:
            return {"ok": False, "msg": "Không mở được cổng serial"}, 500
        return {"ok": True, "mode": "serial"}
    else:
        return {"ok": True, "mode": "noserial"}

@app.post("/session/stop")
@login_required
def session_stop():
    if SERIAL_ENABLED:
        stop_serial_reader()
    return {"ok": True}

# =========================================================
# FIRESTORE TEST (tùy chọn)
# =========================================================
@app.route("/save_patient", methods=["POST"])
def save_patient():
    data = request.get_json(force=True) or {}
    code = data.get("code") or f"BN{int(time.time())}"
    try:
        fs_client.collection("patients").document(code).set(data)
        return {"ok": True, "code": code}
    except Exception as e:
        print("Lỗi Firestore:", e)
        return {"ok": False, "error": str(e)}, 500

# =========================================================
# SIMPLE HTML DEMO DASHBOARD (Realtime SocketIO)
# =========================================================
DASH_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IMU Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="p-4 bg-light">
  <h3>IMU Dashboard - {{username}}</h3>
  <table class="table w-auto bg-white">
    <thead><tr><th>Hip</th><th>Knee</th><th>Ankle</th></tr></thead>
    <tbody id="tblAngles"><tr><td>--</td><td>--</td><td>--</td></tr></tbody>
  </table>

  <button id="btnStart" class="btn btn-primary">Bắt đầu đo</button>
  <button id="btnStop" class="btn btn-secondary">Kết thúc đo</button>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
  <script>
    const socket = io();
    socket.on("imu_data", msg=>{
      const tr = document.querySelector("#tblAngles tr");
      const tds = tr.querySelectorAll("td");
      if(tds.length>=3){
        tds[0].textContent = Number(msg.hip).toFixed(2);
        tds[1].textContent = Number(msg.knee).toFixed(2);
        tds[2].textContent = Number(msg.ankle).toFixed(2);
      }
    });
    document.getElementById("btnStart").onclick = async ()=>{
      const r = await fetch("/session/start",{method:"POST"}); const j = await r.json();
      if(!j.ok) alert(j.msg||"Không start được");
    };
    document.getElementById("btnStop").onclick = async ()=>{
      await fetch("/session/stop",{method:"POST"}); alert("Đã dừng đo");
    };
  </script>
</body></html>
"""

LOGIN_HTML = """
<!doctype html><html><head>
<meta charset="utf-8"><title>Đăng nhập</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head><body class="d-flex align-items-center justify-content-center bg-light" style="height:100vh">
<form method="post" class="card p-4 shadow" style="min-width:320px">
<h4 class="mb-3 text-center">Đăng nhập hệ thống IMU</h4>
{% with messages = get_flashed_messages(with_categories=true) %}
{% for c,m in messages %}<div class="alert alert-{{c}}">{{m}}</div>{% endfor %}
{% endwith %}
<input name="username" class="form-control mb-2" placeholder="Tài khoản" required>
<input name="password" type="password" class="form-control mb-3" placeholder="Mật khẩu" required>
<button class="btn btn-primary w-100">Đăng nhập</button>
</form>
</body></html>
"""

# =========================================================
#  RUN APP
# =========================================================
if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=True,
        allow_unsafe_werkzeug=True
    )
