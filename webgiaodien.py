
# webgiaodien.py
import os, json, time, csv
from datetime import datetime
import threading
from collections import defaultdict
# Chỉ bật đọc cổng COM khi chạy ở máy local (Render không có COM)
SERIAL_ENABLED = os.environ.get("ENABLE_SERIAL", "0") == "1"
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

def start_serial_reader(port="COM6", baud=115200):
    """Đọc dữ liệu serial: id,timestamp,yaw,roll,pitch"""
    global ser, serial_thread, stop_serial_thread

    import serial
    try:
        ser = serial.Serial(port, baud, timeout=0.5)
    except Exception as e:
        print(" Không mở được cổng serial:", e)
        return False

    stop_serial_thread = False
    last_angles = defaultdict(lambda: {"yaw":0,"roll":0,"pitch":0,"ts":0})

    def reader_loop():
        print(f"📡 Đang đọc dữ liệu từ {port} ...")
        while not stop_serial_thread:
            try:
                line = ser.readline().decode("utf-8").strip()
                if not line:
                    continue

                parts = line.split(",")
                if len(parts) < 5:
                    continue

                sid = int(parts[0])
                ts  = float(parts[1])
                yaw = float(parts[2])
                roll = float(parts[3])
                pitch = float(parts[4])

                # Lưu tạm góc của mỗi cảm biến
                last_angles[sid] = {"yaw":yaw,"roll":roll,"pitch":pitch,"ts":ts}

                # Nếu có đủ cả 3 ID (1,2,3) thì gom và emit
                if all(k in last_angles for k in (1,2,3)):
                    hip   = last_angles[1]["pitch"]
                    knee  = last_angles[2]["pitch"]
                    ankle = last_angles[3]["pitch"]
                    t     = last_angles[1]["ts"]

                    # Gọi append_samples (như trước)
                    append_samples([{
                        "t_ms": t,
                        "hip": hip,
                        "knee": knee,
                        "ankle": ankle
                    }])

            except Exception as e:
                print("Serial read error:", e)

        print("Dừng đọc serial")

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
    """
    Tìm file firebase-key.json ở các nơi thường gặp:
    - Render Secret Files: /etc/secrets/firebase-key.json
    - Render biến RENDER_SECRETS_DIR
    - Local: firebase-key.json đặt cạnh code
    - GOOGLE_APPLICATION_CREDENTIALS (nếu có)
    """
    candidates = [
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        "/etc/secrets/firebase-key.json",
        os.path.join(os.environ.get("RENDER_SECRETS_DIR", ""), "firebase-key.json"),
        os.path.join(os.getcwd(), "firebase-key.json"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    raise FileNotFoundError("firebase-key.json not found in expected locations.")

CRED_PATH = find_firebase_key()
cred = credentials.Certificate(CRED_PATH)
firebase_admin.initialize_app(cred)
fs_client = firestore.client()


# ===================== App & Auth =====================
app = Flask(__name__)
app.secret_key = "CHANGE_ME"   # nhớ đổi khi deploy
PATIENTS_FILE = "sample.json"

socketio = SocketIO(app, cors_allowed_origins="*")
# ============ Emit dữ liệu realtime ============
data_buffer = []

def append_samples(samples):
    """Gửi dữ liệu IMU realtime sang client qua Socket.IO"""
    global data_buffer
    for s in samples:
        data_buffer.append(s)
        socketio.emit("imu_data", {
            "t": s.get("t_ms"),
            "hip": s.get("hip"),
            "knee": s.get("knee"),
            "ankle": s.get("ankle"),
        })

login_manager = LoginManager(app)
login_manager.login_view = "login"

USERS = {"komlab": generate_password_hash("123456")}  # đổi khi deploy

# Map bài tập -> đường dẫn video (trong static/videos/)
EXERCISE_VIDEOS = {
    "ankle flexion": "/static/videos/ankle flexion.mp4",
    "hip flexion":   "/static/videos/hip flexion.mp4",
    "knee flexion":  "/static/videos/knee flexion.mp4",
}

class User(UserMixin):
    def __init__(self, u): self.id = u

@login_manager.user_loader
def load_user(u): return User(u) if u in USERS else None

# ===================== Serial placeholders =====================
ser = None
running = False
reader_thread = None
collecting = False
data_buffer = []

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

@app.post("/session/start")
@login_required
def session_start():
    global data_buffer
    data_buffer = []
    if SERIAL_ENABLED:
        ok = start_serial_reader(
            port=os.environ.get("SERIAL_PORT", "COM6"),
            baud=115200
        )
        if not ok:
            return {"ok": False, "msg": "Không mở được cổng serial"}, 500
        return {"ok": True, "mode": "serial"}
    else:
        # Trên Render (không có COM) vẫn trả ok để UI hoạt động
        return {"ok": True, "mode": "noserial"}


@app.post("/session/stop")
@login_required
def session_stop():
    if SERIAL_ENABLED:
        stop_serial_reader()
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
        full_name   = request.form.get("full_name", "").strip()
        national_id = request.form.get("national_id", "").strip()
        dob         = request.form.get("dob", "").strip()
        sex         = request.form.get("sex", "").strip()
        weight      = request.form.get("weight", "").strip()
        height      = request.form.get("height", "").strip()

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
    if serial is None:
        return {"ports": []}
    items = [{"device": p.device, "desc": p.description} for p in serial.tools.list_ports.comports()]
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
    if sex.lower().startswith("m"): sex = "Male"
    elif sex.lower().startswith("f"): sex = "FeMale"

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
    return render_template_string(CALIBRATION_HTML, username=current_user.id)

@app.route("/charts")
@login_required
def charts():
    return "<h3 style='font-family:system-ui;padding:16px'>Trang Biểu đồ (đang phát triển)</h3>"

@app.route("/settings")
@login_required
def settings():
    return "<h3 style='font-family:system-ui;padding:16px'>Trang Cài đặt (đang phát triển)</h3>"

# ===================== HTML =====================
LOGIN_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập IMU</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head><body class="bg-light d-flex align-items-center" style="min-height:100vh">
<div class="container"><div class="row justify-content-center"><div class="col-sm-10 col-md-6 col-lg-4">
<div class="card shadow"><div class="card-body">
<h4 class="mb-3 text-center">Đăng nhập hệ thống IMU</h4>
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for c,m in messages %}<div class="alert alert-{{c}}">{{m}}</div>{% endfor %}
{% endwith %}
<form method="post">
  <div class="mb-3"><label class="form-label">Tài khoản</label><input name="username" class="form-control" required></div>
  <div class="mb-3"><label class="form-label">Mật khẩu</label><input name="password" type="password" class="form-control" required></div>
  <button class="btn btn-primary w-100">Đăng nhập</button>
</form>
<hr><a class="btn btn-outline-secondary w-100" href="https://sites.google.com">← Trang giới thiệu</a>

</div></div></div></div></div></body></html>
"""

# ======= Patients List (Xem lại) – sidebar thu gọn kiểu hiệu chuẩn =======
PATIENTS_LIST_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Danh sách bệnh nhân</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{ --blue:#1669c9; --sbw:260px; }
body{ background:#f7f9fc }

/* Layout + sidebar đồng bộ */
.layout{ display:flex; gap:16px; position:relative; }
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px; border-bottom-right-radius:16px;
  padding:16px; width:var(--sbw); min-height:100%;
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
.menu-btn{
  width:100%; display:block; background:#1973d4; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600;
  text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; color:#fff }

/* Compact */
.compact .container-fluid{ max-width:1280px; margin-inline:auto; }
.compact .row.g-3{ --bs-gutter-x:1rem; --bs-gutter-y:1rem; }
</style>
</head>
<body class="compact sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Danh sách bệnh nhân</span>
    <div class="ms-auto">
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
<title>Create new patient</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
.card{ border-radius:16px; box-shadow:0 8px 20px rgba(16,24,40,.06) }
.btn-outline-thick{ border:2px solid #151515; border-radius:12px; background:#fff; font-weight:600; }
.form-label{ font-weight:600; color:#274b6d }
</style>
</head><body class="bg-light">
<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid">
    <span class="navbar-brand">Thêm bệnh nhân mới</span>
    <div class="ms-auto"><a class="btn btn-outline-secondary" href="/patients">← Danh sách</a></div>
  </div>
</nav>

<div class="container my-3" style="max-width:720px">
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% for c,m in messages %}<div class="alert alert-{{c}}">{{m}}</div>{% endfor %}
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
<style>
:root{ --blue:#1669c9; --soft:#f3f7ff; --sbw:260px; --video-h:360px; }
body{ background:#fafbfe }
.layout{ display:flex; gap:16px; position:relative; }

/* Sidebar */
.sidebar{ background:var(--blue); color:#fff; border-top-right-radius:16px; border-bottom-right-radius:16px; padding:16px; width:var(--sbw); min-height:100%; box-sizing:border-box; }
.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); transition:flex-basis .28s ease, max-width .28s ease, transform .28s ease; will-change:flex-basis,max-width,transform; }
.main-col{ flex:1 1 auto; min-width:0; }

/* Thu gọn mặc định */
.sb-collapsed .sidebar-col{ flex-basis:0; max-width:0; transform:translateX(-8px); }
.sb-collapsed .sidebar{ padding:0; width:0; border-radius:0; }
.sb-collapsed .sidebar *{ display:none; }

.panel{ background:#fff; border-radius:16px; box-shadow:0 8px 20px rgba(16,24,40,.06); padding:16px; }
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
</style>
</head>
<body class="compact sb-collapsed">
<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Xin chào, {{username}}</span>
    <div class="ms-auto d-flex align-items-center gap-3">
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
              <a class="btn btn-outline-thick flex-fill" href="/patients">Danh sách bệnh nhân</a>
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
              <div class="col-6"><label class="form-label">Họ và tên :</label><input class="form-control"></div>
              <div class="col-6"><label class="form-label">Ngày sinh :</label><input type="date" class="form-control"></div>
              <div class="col-6"><label class="form-label">CCCD :</label><input class="form-control"></div>
              <div class="col-6"><label class="form-label">Giới tính :</label><select class="form-select"><option>Nam</option><option>Nữ</option><option>Khác</option></select></div>
              <div class="col-6"><label class="form-label">Cân nặng :</label><input class="form-control"></div>
              <div class="col-6"><label class="form-label">Chiều cao :</label><input class="form-control"></div>
              <div class="col-8"><label class="form-label">Bài kiểm tra :</label>
                <div class="input-group">
                  <select class="form-select" id="exerciseSelect">
                    <option>ankle flexion</option>
                    <option>knee flexion</option>
                    <option>hip flexion</option>
                  </select>
                  <button class="btn btn-outline-thick">Thêm bài tập</button>
                </div>
              </div>
              <div class="col-4"><label class="form-label">Ngày đo :</label><input type="date" class="form-control"></div>
            </div>
          </div>

          <video id="guideVideo" class="w-100" controls playsinline preload="metadata" poster="">
            Sorry, your browser doesn’t support embedded videos.
          </video>
        </div>

       <!-- MÔ PHỎNG GÓC GẬP 2D (ở TRÊN) -->
<div class="col-lg-7 pull-up-guide">
  <div class="panel">
    <div class="d-flex align-items-center justify-content-between mb-2">
      <span class="title-chip">MÔ PHỎNG GÓC GẬP 2D</span>
      <div class="small text-muted">Nguồn: pitch từ IMU (đơn vị °)</div>
    </div>
    <canvas id="angleCanvas" width="520" height="360" style="width:100%;max-width:720px;display:block;margin:auto;"></canvas>
    <div class="text-center mt-2">
      <span class="badge text-bg-light border me-2">Hip: <span id="liveHip">--</span>°</span>
      <span class="badge text-bg-light border me-2">Knee: <span id="liveKnee">--</span>°</span>
      <span class="badge text-bg-light border">Ankle: <span id="liveAnkle">--</span>°</span>
    </div>
  </div>
</div>

<!-- CỤM NÚT BẮT ĐẦU/KẾT THÚC/ LƯU (giữ nguyên) -->
<div class="col-lg-5">
  <div class="panel d-grid gap-3">
    <button class="btn btn-outline-thick py-3" id="btnStart">Bắt đầu đo</button>
    <button class="btn btn-outline-thick py-3" id="btnStop">Kết thúc đo</button>
    <button class="btn btn-outline-thick py-3" id="btnSave">Lưu kết quả</button>
  </div>
</div>

<!-- HƯỚNG DẪN QUY TRÌNH ĐO (đưa XUỐNG DƯỚI) -->
<div class="col-lg-12">
  <div class="panel">
    <div class="text-center mb-3"><span class="title-chip">HƯỚNG DẪN QUY TRÌNH ĐO</span></div>
    <div class="row g-2">
      <div class="col-md-3"><div class="panel">Bước 1: Hiệu chuẩn thiết bị</div></div>
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

<script>
const videosMap = {{ videos|tojson }};
const sel = document.getElementById('exerciseSelect');
const vid = document.getElementById('guideVideo');
function updateVideo() {
  const key = sel.value, url = videosMap[key];
  if (!url) { vid.removeAttribute('src'); vid.load(); return; }
  if (vid.src !== location.origin + url) { vid.src = url; vid.load(); }
  vid.play().catch(()=>{});
}
sel.addEventListener('change', updateVideo);
updateVideo();

document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});
</script>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js" crossorigin="anonymous"></script>
<script>
  // Kết nối socket: GÁN vào window để tái dùng ở nơi khác
  window.socket = window.socket || io();
  const socket = window.socket;

  // Nhận dữ liệu realtime & đổ vào bảng 3 cột đã có
  socket.on("imu_data", (msg) => {
    const tr = document.querySelector("#tblAngles tr");
    if (!tr) return;
    const tds = tr.querySelectorAll("td");
    if (tds.length >= 3) {
      if (msg.hip   != null) tds[0].textContent = Number(msg.hip).toFixed(2);
      if (msg.knee  != null) tds[1].textContent = Number(msg.knee).toFixed(2);
      if (msg.ankle != null) tds[2].textContent = Number(msg.ankle).toFixed(2);
    }
  });

  // Gắn vào đúng 2 nút sẵn có
  const btnStart = document.getElementById("btnStart");
  const btnStop  = document.getElementById("btnStop");

  if (btnStart) btnStart.addEventListener("click", async () => {
    const r = await fetch("/session/start", { method: "POST" });
    const j = await r.json();
    if (!j.ok) alert(j.msg || "Không start được phiên đo");
  });

  if (btnStop) btnStop.addEventListener("click", async () => {
    const r = await fetch("/session/stop", { method: "POST" });
    const j = await r.json();
    if (!j.ok) alert(j.msg || "Không stop được phiên đo");
  });
</script>
<script>
(function(){
  const canvas = document.getElementById("angleCanvas");
  if (!canvas) return;

  const ctx  = canvas.getContext("2d");
  let hipDeg = 0, kneeDeg = 0, ankleDeg = 0;

  const LEN  = { thigh: 140, shank: 130, foot: 70 };
  const WIDTH= { thigh: 18,  shank: 12,  foot: 10 };

  function drawCapsule(x1, y1, x2, y2, w, color) {
    ctx.save();
    ctx.lineCap = "round";
    ctx.strokeStyle = "rgba(0,0,0,.08)";
    ctx.lineWidth = w + 4;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    ctx.strokeStyle = color;
    ctx.lineWidth = w;
    ctx.beginPath(); ctx.moveTo(x1,y1); ctx.lineTo(x2,y2); ctx.stroke();
    ctx.restore();
  }

  function drawJoint(x, y, rOuter=8, rInner=5){
    ctx.save();
    ctx.fillStyle = "rgba(0,0,0,.15)";
    ctx.beginPath(); ctx.arc(x, y, rOuter, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = "#0d6efd";
    ctx.beginPath(); ctx.arc(x, y, rInner, 0, Math.PI*2); ctx.fill();
    ctx.restore();
  }

  function drawModel() {
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);

    const hipX = W/2, hipY = 80;
    const hipRad   = (90 + hipDeg) * Math.PI/180;
    const kneeRad  = hipRad + (kneeDeg  * Math.PI/180);
    const footRad  = kneeRad + (ankleDeg * Math.PI/180);

    const kneeX  = hipX + LEN.thigh * Math.cos(hipRad);
    const kneeY  = hipY + LEN.thigh * Math.sin(hipRad);
    const ankleX = kneeX + LEN.shank * Math.cos(kneeRad);
    const ankleY = kneeY + LEN.shank * Math.sin(kneeRad);
    const toeX   = ankleX + LEN.foot  * Math.cos(footRad);
    const toeY   = ankleY + LEN.foot  * Math.sin(footRad);

    ctx.save();
    ctx.fillStyle = "#6c757d";
    ctx.beginPath(); ctx.arc(hipX, hipY, 9, 0, Math.PI*2); ctx.fill();
    ctx.restore();

    drawCapsule(hipX, hipY, kneeX,  kneeY,  WIDTH.thigh, "#0d6efd");
    drawCapsule(kneeX, kneeY, ankleX, ankleY, WIDTH.shank, "#1973d4");
    drawCapsule(ankleX, ankleY, toeX, toeY,  WIDTH.foot,  "#5aa0ff");

    drawJoint(kneeX,  kneeY);
    drawJoint(ankleX, ankleY, 7, 4);

    ctx.fillStyle = "#212529";
    ctx.font = "14px system-ui";
    ctx.fillText(`Hip: ${hipDeg.toFixed(1)}°`,  hipX - 70, hipY - 16);
    ctx.fillText(`Knee: ${kneeDeg.toFixed(1)}°`, kneeX + 12, kneeY + 4);
    ctx.fillText(`Ankle: ${ankleDeg.toFixed(1)}°`, ankleX + 12, ankleY + 4);

    const elHip = document.getElementById("liveHip");
    const elKnee = document.getElementById("liveKnee");
    const elAnkle = document.getElementById("liveAnkle");
    if (elHip)   elHip.textContent   = hipDeg.toFixed(1);
    if (elKnee)  elKnee.textContent  = kneeDeg.toFixed(1);
    if (elAnkle) elAnkle.textContent = ankleDeg.toFixed(1);
  }

  // Vẽ lần đầu
  drawModel();

  // Đăng ký socket **BÊN TRONG** IIFE, KHÔNG dùng sock.off để tránh xóa listener khác
  const sock = window.socket;
  if (sock) {
    sock.on("imu_data", (msg) => {
      if (typeof msg.hip   === "number") hipDeg   = msg.hip;
      if (typeof msg.knee  === "number") kneeDeg  = msg.knee;
      if (typeof msg.ankle === "number") ankleDeg = msg.ankle;
      drawModel();
    });
  }

  // tiện test từ Console: setDemo(hip,knee,ankle)
  window.setDemo = (h=0,k=0,a=0)=>{ hipDeg=h; kneeDeg=k; ankleDeg=a; drawModel(); };

})();  // <-- chỉ đóng IIFE một lần
</script>

</body></html>
"""

# ======= NEW: Calibration page =======
CALIBRATION_HTML = """
<!doctype html><html lang="vi"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hiệu chuẩn</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{ --blue:#1669c9; --sbw:260px; }
body{ background:#f5f7fb }
.layout{ display:flex; gap:16px; }
.sidebar{ background:var(--blue); color:#fff; border-top-right-radius:16px; border-bottom-right-radius:16px; padding:16px; width:var(--sbw); min-height:100%; box-sizing:border-box; }
.sidebar-col{ flex:0 0 var(--sbw); max-width:var(--sbw); transition:flex-basis .28s ease, max-width .28s ease; }
.main-col{ flex:1 1 auto; min-width:0; }
.sb-collapsed .sidebar-col{ flex-basis:0; max-width:0; }
.sb-collapsed .sidebar{ padding:0; width:0; border-radius:0; }
.sb-collapsed .sidebar *{ display:none; }

#btnToggleSB{ border:2px solid #d8e6ff; border-radius:10px; background:#fff; padding:6px 10px; font-weight:700; }
#btnToggleSB:hover{ background:#f4f8ff; }
.menu-btn{ width:100%; display:block; background:#1973d4; border:none; color:#fff; padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600; text-align:left; text-decoration:none; }
.menu-btn:hover{ background:#1f80ea; color:#fff }

/* Bảng hiệu chuẩn dạng lưới */
.cal-grid{ max-width:1200px; margin-inline:auto; }
.cell{
  height:56px; background:#fff; border:1px solid #e5e7ef; border-radius:10px;
  display:flex; align-items:center; padding:0 14px; box-shadow:0 2px 8px rgba(16,24,40,.04);
}
.cell.title{ font-weight:600; background:#f8fbff; }
.header{ font-weight:700; color:#0a3768; }
.logo-wrap{ display:flex; justify-content:center; align-items:flex-start; height:100%; }
.logo-wrap img{ max-width:160px; }

@media (max-width:991.98px){
  .cell{ height:48px; }
  .logo-wrap{ justify-content:flex-start; }
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
      <div class="cal-grid">
        <!-- Header row -->
        <div class="row g-2 mb-2">
          <div class="col-2"><div class="cell header"> </div></div>
          <div class="col"><div class="cell header">Sys</div></div>
          <div class="col"><div class="cell header">Gyro</div></div>
          <div class="col"><div class="cell header">Acc</div></div>
          <div class="col"><div class="cell header">Mag</div></div>
        </div>

        <!-- Rows IMU1..IMU4 -->
        {% for idx in [1,2,3,4] %}
        <div class="row g-2 mb-2">
          <div class="col-2"><div class="cell title">IMU{{idx}}:</div></div>
          <div class="col"><div class="cell" id="imu{{idx}}-sys"></div></div>
          <div class="col"><div class="cell" id="imu{{idx}}-gyro"></div></div>
          <div class="col"><div class="cell" id="imu{{idx}}-acc"></div></div>
          <div class="col"><div class="cell" id="imu{{idx}}-mag"></div></div>
        </div>
        {% endfor %}
      </div>
    </main>

    <!-- Logo phải -->
    <aside class="d-none d-lg-block" style="width:220px">
      <div class="logo-wrap">
        <img src="/static/unnamed.png" alt="Logo">
      </div>
    </aside>
  </div>
</div>

<script>
document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* (Tùy chọn) Ví dụ nhồi dữ liệu trạng thái */
const fake = ["Sₛ","OK","NG","--"];
["1","2","3","4"].forEach(i=>{
  ["sys","gyro","acc","mag"].forEach(k=>{
    const el = document.getElementById(`imu${i}-${k}`);
    if(el) el.textContent = ""; // để trống sẵn giống mockup
  });
});
</script>
</body></html>
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
body{ background:#f7f9fc }

/* Bố cục & sidebar giống trang Hiệu chuẩn */
.layout{ display:flex; gap:16px; position:relative; }
.sidebar{
  background:var(--blue); color:#fff;
  border-top-right-radius:16px; border-bottom-right-radius:16px;
  padding:16px; width:var(--sbw); min-height:100%;
  box-sizing:border-box;
}
.sidebar-col{
  flex:0 0 var(--sbw);
  max-width:var(--sbw);
  transition:flex-basis .28s ease, max-width .28s ease, transform .28s ease;
  will-change:flex-basis,max-width,transform;
}
.main-col{ flex:1 1 auto; min-width:0; }

/* Mặc định THU GỌN hoàn toàn (như yêu cầu) */
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

/* Menu nút trong sidebar */
.menu-btn{
  width:100%; display:block; background:#1973d4; border:none; color:#fff;
  padding:10px 12px; margin:8px 0; border-radius:12px; font-weight:600;
  text-align:left; text-decoration:none;
}
.menu-btn:hover{ background:#1f80ea; color:#fff }

/* Compact cho trang */
.compact .container-fluid{ max-width:1280px; margin-inline:auto; }
.compact .row.g-3{ --bs-gutter-x:1rem; --bs-gutter-y:1rem; }
.compact .btn-outline-thick{ padding:10px 14px; border-radius:10px; }
</style>
</head>
<body class="compact sb-collapsed">

<nav class="navbar bg-white shadow-sm px-3">
  <div class="container-fluid d-flex align-items-center">
    <button id="btnToggleSB" class="btn me-2">☰</button>
    <span class="navbar-brand mb-0">Thông tin bệnh nhân</span>
    <div class="ms-auto">
      
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
              <div class="col-6">
                <label class="form-label">Cân nặng (kg)</label>
                <input id="weight" class="form-control input-sm">
              </div>

              <div class="col-12">
                <label class="form-label">Mã bệnh nhân</label>
                <input id="patient_code" class="form-control input-sm" placeholder="(để trống để tạo mới)">
              </div>

              <div class="col-12 d-flex justify-content-center gap-4 mt-2">
                <button id="btnSave" class="btn btn-outline-thick py-2 px-5 fs-5">💾 Lưu</button>
                <button id="btnDelete" class="btn btn-outline-thick py-2 px-5 fs-5">🗑️ Xóa</button>
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
/* Toggle sidebar: giống trang Hiệu chuẩn */
document.getElementById('btnToggleSB').addEventListener('click', ()=>{
  document.body.classList.toggle('sb-collapsed');
});

/* ===== Logic quản lý bệnh nhân (giữ nguyên) ===== */
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

    # Lấy mã bệnh nhân hoặc tự sinh nếu thiếu
    code = data.get("code") or f"BN{int(time.time())}"

    # Lưu vào Firestore
    try:
        fs_client.collection("patients").document(code).set(data)
        return {"ok": True, "code": code}
    except Exception as e:
        print("Lỗi khi lưu Firestore:", e)
        return {"ok": False, "error": str(e)}, 500
        
def stop_serial_reader():
    global stop_serial_thread, ser
    stop_serial_thread = True
    if ser:
        try: ser.close()
        except: pass
# ===================== Run =====================
if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080)),
        debug=True,
        allow_unsafe_werkzeug=True
    )








