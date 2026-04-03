import os, sys, time, io, csv, json, webbrowser
from datetime import datetime
from threading import Timer
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash, send_file
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO
import database
import serial_handler
from database import load_records_from_file
# --- THƯ VIỆN AI ---
from langchain.chains import RetrievalQA
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.vectorstores import Chroma
from langchain.llms import LlamaCpp
from dotenv import load_dotenv
import os

load_dotenv()

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.dirname(os.path.abspath(__file__))

    return os.path.join(base_path, relative_path)


app = Flask(__name__, static_folder=resource_path('static'), template_folder=resource_path('templates'))
# --- KHỞI TẠO BỘ NÃO AI (Load 1 lần duy nhất khi bật Web) ---
print("Đang nạp bộ não AI (Card RTX đang làm việc)...")
embeddings = HuggingFaceEmbeddings(model_name=os.environ.get("EMBEDDINGS_MODEL_NAME"))
db = Chroma(persist_directory=os.environ.get('PERSIST_DIRECTORY'), embedding_function=embeddings)
retriever = db.as_retriever(search_kwargs={"k": 2})

llm = LlamaCpp(
    model_path=os.environ.get('MODEL_PATH'),
    n_ctx=4096,
    n_gpu_layers=40, # RTX gánh
    n_threads=4,
    verbose=False
)
qa_chain = RetrievalQA.from_chain_type(llm=llm, chain_type="stuff", retriever=retriever)
print("AI ĐÃ SẴN SÀNG!")
app.secret_key = "CHANGE_ME"

socketio = SocketIO(app, cors_allowed_origins="*", ping_interval=10, ping_timeout=30, async_mode="threading")

# --- Xử lý Login ---
login_manager = LoginManager(app)
login_manager.login_view = "login"
USERS = {"komlab": generate_password_hash("123456")}


class User(UserMixin):
    def __init__(self, u): self.id = u


@login_manager.user_loader
def load_user(u):
    return User(u) if u in USERS else None


EXERCISE_VIDEOS = {
    "ankle flexion": "/static/videos/ankle_flexion.mp4",
    "knee flexion": "/static/videos/knee_flexion.mp4",
    "hip flexion": "/static/videos/hip_flexion.mp4",
}


# ================= ROUTES GIAO DIỆN (HTML) =================
@socketio.on("connect")
def _on_connect():
    socketio.emit("imu_data", {"t": time.time() * 1000, "hip": 0, "knee": 0, "ankle": 0})


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
    return render_template("login.html", error_message=error_message)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", username=current_user.id, videos=EXERCISE_VIDEOS)


@app.route("/calibration")
@login_required
def calibration():
    open_guide = request.args.get("guide", "0") in ("1", "true", "yes")
    return render_template("calibration.html", username=current_user.id, open_guide=open_guide)

@app.route('/api/chat', methods=['POST'])
def api_chat():
    data = request.json
    user_query = data.get('query', '')
    if not user_query:
        return jsonify({'answer': 'Vui lòng nhập câu hỏi.'}), 400

    # Ép AI nói tiếng Việt để thông minh hơn
    prompt = f"Dựa vào tài liệu, hãy trả lời câu hỏi sau bằng tiếng Việt: {user_query}"

    # Nhờ AI xử lý
    res = qa_chain(prompt)
    return jsonify({'answer': res['result']})
@app.route("/records")
@login_required
def records():
    with database.RECORD_LOCK:
        rows = list(database.RECORD_STORE)
    rows.sort(key=lambda r: r.get("created_at_ts", 0), reverse=True)
    for r in rows:
        if "vas_summary" not in r or r["vas_summary"] is None: r["vas_summary"] = {}
    return render_template("records.html", username=current_user.id, records=rows)


@app.route("/patients/manage")
@login_required
def view_patients_manage():
    return render_template("patients_manage.html")


@app.route("/charts")
@login_required
def charts():
    patient_code = request.args.get("patient_code", "").strip()
    exercise_name = request.args.get("exercise", "").strip()

    # Lấy VAS
    vas_before = None
    vas_after = None
    region = "hip" if "hip" in exercise_name.lower() else "knee" if "knee" in exercise_name.lower() else "ankle"

    with database.VAS_LOCK:
        for rec in reversed(database.VAS_STORE):
            if rec.get("exercise_region") != region: continue
            if patient_code and rec.get("patient_code") != patient_code: continue
            ph = rec.get("phase")
            if ph == "before" and vas_before is None:
                vas_before = rec.get("vas")
            elif ph == "after" and vas_after is None:
                vas_after = rec.get("vas")
            if vas_before is not None and vas_after is not None: break

    if not serial_handler.LAST_SESSION:
        return render_template("charts.html", username=current_user.id, t_ms=[], hip=[], knee=[], ankle=[], emg=[],
                               emg_rms=[], emg_env=[], patient_code=patient_code, exercise_name=exercise_name,
                               vas_before=vas_before, vas_after=vas_after)

    rows = sorted(list(serial_handler.LAST_SESSION), key=lambda x: x["t_ms"])
    t0 = rows[0]["t_ms"] if rows else 0
    t_ms = [round((r["t_ms"] - t0) / 1000.0, 3) for r in rows]

    hipArr = [r.get("hip", 0.0) for r in rows]
    kneeArr = [r.get("knee", 0.0) for r in rows]
    ankleArr = [r.get("ankle", 0.0) for r in rows]

    return render_template("charts.html", username=current_user.id, t_ms=t_ms, hip=hipArr, knee=kneeArr, ankle=ankleArr,
                           emg=[], emg_rms=[], emg_env=[], patient_code=patient_code, exercise_name=exercise_name,
                           vas_before=vas_before, vas_after=vas_after)


@app.route("/charts_emg")
@login_required
def charts_emg():
    return render_template("emg_chart.html", username=current_user.id)


@app.route("/settings")
@login_required
def settings_page():
    return render_template("settings.html", username=current_user.id)


# ================= ROUTES API (Xử lý Data/Hardware) =================
@app.route("/ports")
@login_required
def ports():
    if not serial_handler.list_ports: return jsonify(ports=[])
    return jsonify(ports=[{"device": p.device, "desc": p.description} for p in serial_handler.list_ports.comports()])


@app.post("/session/start")
@login_required
def session_start():
    serial_handler.data_buffer = []
    serial_handler.reset_max_angles()
    if serial_handler.SERIAL_ENABLED:
        port = os.environ.get("SERIAL_PORT") or "COM3"
        baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        # Truyền socketio vào đây
        if not serial_handler.start_serial_reader(socketio, port=port, baud=baud):
            return jsonify(ok=False, msg=f"Không mở được cổng serial (port={port})"), 500
        return jsonify(ok=True, mode="serial", port=port, baud=baud)
    return jsonify(ok=True, mode="noserial")


@app.post("/session/stop")
@login_required
def session_stop():
    if serial_handler.SERIAL_ENABLED: serial_handler.stop_serial_reader()
    with serial_handler.DATA_LOCK:
        serial_handler.LAST_SESSION = list(serial_handler.data_buffer)
        serial_handler.data_buffer.clear()
    return jsonify(ok=True, msg="Đã kết thúc phiên đo")


@app.get("/api/patients")
@login_required
def api_patients_all():
    rows, raw = database.load_patients_rows()
    return jsonify(rows=rows, raw=raw)


@app.post("/api/patients")
@login_required
def api_patients_save():
    data = request.get_json(force=True) or {}
    code = (data.get("patient_code") or "").strip()
    full_name = (data.get("name") or "").strip()
    if not full_name: return jsonify(ok=False, msg="Thiếu họ tên"), 400

    _, raw = database.load_patients_rows()
    if not code: code = database.gen_patient_code(full_name)

    raw[code] = {
        "DateOfBirth": data.get("dob", ""),
        "Gender": "Male" if data.get("gender", "").lower().startswith("m") else "FeMale",
        "Height": data.get("height", ""), "ID": data.get("national_id", ""),
        "PatientCode": code, "Weight": data.get("weight", ""), "name": full_name
    }
    database.save_patients_data(raw)
    return jsonify(ok=True, patient_code=code)

load_records_from_file()
@app.post("/api/save_record")
@login_required
def api_save_record():
    data = request.get_json(force=True) or {}
    now = datetime.now(database.VN_TZ)
    record = {
        "created_at_ts": now.timestamp(),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "patient_code": data.get("patient_code", ""),
        "measure_date": data.get("measure_date", ""),
        "patient_info": data.get("patient_info", {}),
        "exercise_scores": data.get("exercise_scores", {}),
        "vas_summary": {}
    }
    with database.RECORD_LOCK:
        database.RECORD_STORE.append(record)
        database.save_records_to_file()
    return jsonify(ok=True, msg="saved", record=record)


@app.post("/api/delete_record")
@login_required
def api_delete_record():
    data = request.get_json(silent=True) or {}
    idx = data.get("index")
    with database.RECORD_LOCK:
        if idx is not None and 0 <= idx < len(database.RECORD_STORE):
            database.RECORD_STORE.pop(idx)
            database.save_records_to_file()
            return jsonify(ok=True)
    return jsonify(ok=False, msg="Index không hợp lệ"), 400


@app.post("/save_vas")
def save_vas():
    data = request.get_json(silent=True) or {}
    rec = {
        "patient_code": data.get("patient_code"),
        "exercise_name": data.get("exercise_name"),
        "exercise_region": data.get("exercise_region"),
        "phase": data.get("phase"),
        "vas": float(data.get("vas", 0)),
        "ts": time.time(),
    }
    with database.VAS_LOCK:
        database.VAS_STORE.append(rec)
    return jsonify(ok=True)


if __name__ == "__main__":
    database.load_records_from_file()

    port = int(os.environ.get("PORT", 8080))


    def open_browser(): webbrowser.open_new(f"http://127.0.0.1:{port}")


    Timer(1.5, open_browser).start()

    print(f"Đang chạy server tại cổng {port}...")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)