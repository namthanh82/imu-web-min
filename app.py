import importlib.util
import os
import time
import webbrowser
from pathlib import Path
from threading import Lock, Timer

from dotenv import load_dotenv
from flask import flash, jsonify, redirect, render_template_string, request, url_for
from flask_login import current_user, login_required

import database
import webgiaodien


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

app = webgiaodien.app
socketio = webgiaodien.socketio
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "CHANGE_ME")

def env_path(name: str, default: str | None = None) -> Path | None:
    raw_value = os.environ.get(name, default)
    if not raw_value:
        return None

    path = Path(raw_value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

webgiaodien.EMG_CHART_HTML = webgiaodien.EMG_CHART_HTML.replace(
    "const emg_env_raw = {{ (emg_env or []) | tojson }};\n\n/*",
    """const emg_env_raw = {{ (emg_env or []) | tojson }};

let t_ms     = (t_ms_raw    || []).slice();
let hipArr   = (hip_raw     || []).slice();
let kneeArr  = (knee_raw    || []).slice();
let ankleArr = (ankle_raw   || []).slice();
let emgArr    = (emg_raw     || []).slice();
let emgRmsArr = (emg_rms_raw || []).slice();
let emgEnvArr = (emg_env_raw || []).slice();

/*""",
)
webgiaodien.EMG_CHART_HTML = webgiaodien.EMG_CHART_HTML.replace(
    "rms_len: emgRms.length,",
    "rms_len: emgRmsArr.length,",
)
webgiaodien.EMG_CHART_HTML = webgiaodien.EMG_CHART_HTML.replace(
    "env_len: emgEnv.length,",
    "env_len: emgEnvArr.length,",
)
webgiaodien.EMG_CHART_HTML = webgiaodien.EMG_CHART_HTML.replace(
    "if (emgRms && emgRms.length) datasets.push({ label:\"rms\", data: emgRms, borderWidth:2, tension:0.15 });",
    "if (emgRmsArr && emgRmsArr.length) datasets.push({ label:\"rms\", data: emgRmsArr, borderWidth:2, tension:0.15 });",
)
webgiaodien.EMG_CHART_HTML = webgiaodien.EMG_CHART_HTML.replace(
    "if (emgEnv && emgEnv.length) datasets.push({ label:\"env\", data: emgEnv, borderWidth:2, tension:0.15 });",
    "if (emgEnvArr && emgEnvArr.length) datasets.push({ label:\"env\", data: emgEnvArr, borderWidth:2, tension:0.15 });",
)

AI_LOCK = Lock()
AI_STATE = {"qa_chain": None, "error": None}
AI_DEPENDENCIES = {
    "langchain": "langchain",
    "chromadb": "chromadb",
    "llama-cpp-python": "llama_cpp",
    "sentence-transformers": "sentence_transformers",
}


def init_ai_chain():
    with AI_LOCK:
        if AI_STATE["qa_chain"] is not None:
            return AI_STATE["qa_chain"]

        model_path = env_path("MODEL_PATH")
        persist_directory = env_path("PERSIST_DIRECTORY", "db")

        if not model_path or not model_path.exists():
            AI_STATE["error"] = "Model file was not found. Check MODEL_PATH in .env."
            return None

        if persist_directory is None:
            AI_STATE["error"] = "PERSIST_DIRECTORY is not configured."
            return None

        persist_directory.mkdir(parents=True, exist_ok=True)
        if not any(persist_directory.iterdir()):
            AI_STATE["error"] = "Vector store is empty. Add files to source_documents/ and run python ingest.py."
            return None

        missing_dependencies = [
            package_name
            for package_name, module_name in AI_DEPENDENCIES.items()
            if importlib.util.find_spec(module_name) is None
        ]
        if missing_dependencies:
            AI_STATE["error"] = (
                "Missing AI packages: "
                + ", ".join(missing_dependencies)
                + ". Install them with python -m pip install -r requirements-ai.txt."
            )
            return None

        try:
            from langchain.chains import RetrievalQA
            from langchain.embeddings import HuggingFaceEmbeddings
            from langchain.llms import LlamaCpp
            from langchain.vectorstores import Chroma
        except Exception as exc:
            AI_STATE["error"] = f"AI imports failed: {exc}"
            return None

        try:
            embeddings = HuggingFaceEmbeddings(
                model_name=os.environ.get(
                    "EMBEDDINGS_MODEL_NAME",
                    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                )
            )
            vector_db = Chroma(
                persist_directory=str(persist_directory),
                embedding_function=embeddings,
            )
            retriever = vector_db.as_retriever(search_kwargs={"k": 2})
            llm = LlamaCpp(
                model_path=str(model_path),
                n_ctx=int(os.environ.get("MODEL_N_CTX", "4096")),
                n_gpu_layers=int(os.environ.get("MODEL_N_GPU_LAYERS", "40")),
                n_threads=int(os.environ.get("MODEL_N_THREADS", "4")),
                verbose=False,
            )
            AI_STATE["qa_chain"] = RetrievalQA.from_chain_type(
                llm=llm,
                chain_type="stuff",
                retriever=retriever,
            )
            AI_STATE["error"] = None
            return AI_STATE["qa_chain"]
        except Exception as exc:
            AI_STATE["error"] = f"AI initialization failed: {exc}"
            return None


def latest_session_series() -> dict[str, list[float]]:
    rows = list(webgiaodien.LAST_SESSION)
    rows.sort(key=lambda row: row["t_ms"])

    if not rows:
        return {
            "t_ms": [],
            "hip": [],
            "knee": [],
            "ankle": [],
            "emg": [],
            "emg_rms": [],
            "emg_env": [],
        }

    t0 = rows[0]["t_ms"]
    return {
        "t_ms": [round((row["t_ms"] - t0) / 1000.0, 3) for row in rows],
        "hip": [row.get("hip", 0.0) for row in rows],
        "knee": [row.get("knee", 0.0) for row in rows],
        "ankle": [row.get("ankle", 0.0) for row in rows],
        "emg": [row.get("emg", 0.0) or 0.0 for row in rows],
        "emg_rms": [row.get("emg_rms", 0.0) or 0.0 for row in rows],
        "emg_env": [row.get("emg_env", 0.0) or 0.0 for row in rows],
    }


@login_required
def session_start():
    webgiaodien.data_buffer = []
    webgiaodien.reset_max_angles()

    if webgiaodien.SERIAL_ENABLED:
        port = (
            os.environ.get("SERIAL_PORT")
            or webgiaodien.auto_detect_port()
            or ("COM3" if os.name == "nt" else "/dev/ttyUSB0")
        )
        baud = int(os.environ.get("SERIAL_BAUD", "115200"))
        ok = webgiaodien.start_serial_reader(port=port, baud=baud)
        if not ok:
            return jsonify(ok=False, msg=f"Không mở được cổng serial (port={port})"), 500
        return jsonify(ok=True, mode="serial", port=port, baud=baud)

    return jsonify(ok=True, mode="noserial")


app.view_functions["session_start"] = session_start


@app.route("/patients/manage")
@login_required
def patients_manage():
    return render_template_string(webgiaodien.PATIENTS_MANAGE_HTML, username=current_user.id)


@app.route("/patients/new", methods=["GET", "POST"])
@login_required
def patients_new():
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        if not full_name:
            flash("Thiếu họ tên", "danger")
            return render_template_string(webgiaodien.PATIENT_NEW_HTML)

        _, raw = database.load_patients_rows()
        patient_code = database.gen_patient_code(full_name)
        raw[patient_code] = {
            "DateOfBirth": request.form.get("dob", "").strip(),
            "Gender": request.form.get("sex", "").strip(),
            "Height": request.form.get("height", "").strip(),
            "ID": request.form.get("national_id", "").strip(),
            "PatientCode": patient_code,
            "Weight": request.form.get("weight", "").strip(),
            "name": full_name,
        }
        database.save_patients_data(raw)
        flash(f"Đã lưu bệnh nhân {patient_code}", "success")
        return redirect(url_for("patients_manage"))

    return render_template_string(webgiaodien.PATIENT_NEW_HTML)


@app.route("/patients")
@login_required
def patients_review():
    return redirect(url_for("patients_manage"))


@app.delete("/api/patients/<code>")
@login_required
def api_patients_delete(code: str):
    _, raw = database.load_patients_rows()
    if code not in raw:
        return jsonify(ok=False, msg="Không tìm thấy bệnh nhân"), 404

    raw.pop(code, None)
    database.save_patients_data(raw)
    return jsonify(ok=True)


@app.delete("/api/patients")
@login_required
def api_patients_delete_all():
    database.save_patients_data({})
    return jsonify(ok=True)


@login_required
def api_patients_save():
    data = request.get_json(force=True) or {}
    patient_code = (data.get("patient_code") or "").strip()
    full_name = (data.get("name") or "").strip()
    if not full_name:
        return jsonify(ok=False, msg="Thiếu họ tên"), 400

    _, raw = database.load_patients_rows()
    if not patient_code:
        patient_code = database.gen_patient_code(full_name)

    gender = (data.get("gender") or "").strip()
    if gender.lower().startswith("m"):
        gender = "Male"
    elif gender.lower().startswith("f"):
        gender = "Female"

    raw[patient_code] = {
        "DateOfBirth": data.get("dob", ""),
        "Gender": gender,
        "Height": data.get("height", ""),
        "ID": data.get("national_id", ""),
        "PatientCode": patient_code,
        "Weight": data.get("weight", ""),
        "name": full_name,
    }
    database.save_patients_data(raw)
    return jsonify(ok=True, patient_code=patient_code)


app.view_functions["api_patients_save"] = api_patients_save


@app.route("/charts_emg")
@login_required
def charts_emg():
    return render_template_string(
        webgiaodien.EMG_CHART_HTML,
        username=current_user.id,
        **latest_session_series(),
    )


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(silent=True) or {}
    user_query = data.get("query", "").strip()
    if not user_query:
        return jsonify({"answer": "Vui lòng nhập câu hỏi."}), 400

    qa_chain = init_ai_chain()
    if qa_chain is None:
        return jsonify({"answer": AI_STATE["error"]}), 503

    prompt = f"Dựa vào tài liệu, hãy trả lời câu hỏi sau bằng tiếng Việt: {user_query}"
    try:
        result = qa_chain(prompt)
    except Exception as exc:
        return jsonify({"answer": f"AI query failed: {exc}"}), 500

    return jsonify({"answer": result["result"]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))

    if os.environ.get("AUTO_OPEN_BROWSER", "1") == "1":
        Timer(1.5, lambda: webbrowser.open_new(f"http://127.0.0.1:{port}")).start()

    print(f"Server listening on port {port}.")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
