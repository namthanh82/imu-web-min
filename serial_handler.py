import threading, time, math, os
from collections import defaultdict, deque

# -- Khai báo PySerial --
pyserial = None
list_ports = None
SERIAL_ENABLED = True
try:
    import serial as pyserial
    from serial.tools import list_ports
except Exception:
    SERIAL_ENABLED = False

# -- Biến toàn cục phần cứng --
ser = None
serial_thread = None
stop_serial_thread = False

DATA_LOCK = threading.Lock()
MAX_LOCK = threading.Lock()

data_buffer = []
LAST_SESSION = []
MAX_ANGLES = {"hip": 0.0, "knee": 0.0, "ankle": 0.0}

HIP_STATE = {"mode": "front", "prev_pitch2": 0.0}
PITCH_MID = 90.0
PITCH_HYS = 10.0
HIP_CROSS_TH = 40.0
DEADZONE = 2.0

_SMOOTH_STATE = {"hip": 0.0, "knee": 0.0, "ankle": 0.0}
_SMOOTH_ALPHA = {"hip": 0.25, "knee": 0.25, "ankle": 0.25}


def norm_deg(x: float) -> float:
    while x > 180: x -= 360
    while x < -180: x += 360
    return x


def clamp(val, lo, hi):
    return max(lo, min(hi, val))


def _smooth(key: str, x: float) -> float:
    a = _SMOOTH_ALPHA.get(key, 0.25)
    prev = _SMOOTH_STATE.get(key, x)
    y = a * x + (1 - a) * prev
    _SMOOTH_STATE[key] = y
    return y


def reset_max_angles():
    with MAX_LOCK:
        MAX_ANGLES["hip"] = 0.0
        MAX_ANGLES["knee"] = 0.0
        MAX_ANGLES["ankle"] = 0.0


def auto_detect_port():
    if not list_ports: return None
    ports = list(list_ports.comports())
    for p in ports:
        if any(x in (p.description or "").upper() for x in ["USB", "ACM", "CP210", "CH340", "UART", "SERIAL"]):
            return p.device
    return ports[0].device if ports else None


def parse_serial_line(line: str):
    parts = [p.strip() for p in line.strip().split(",") if p.strip() != ""]
    if not parts: return None
    tag = parts[0].upper()
    try:
        if tag == "IMU" and len(parts) >= 6:
            return ("imu", int(parts[1]), int(float(parts[2])), float(parts[3]), float(parts[4]), float(parts[5]))
        if tag == "EMG" and len(parts) >= 4:
            return ("emg", int(parts[1]), int(float(parts[2])), float(parts[3]))
    except Exception:
        return None
    return None


def stop_serial_reader():
    global ser, serial_thread, stop_serial_thread
    stop_serial_thread = True
    try:
        if ser is not None: ser.close()
    except Exception:
        pass
    finally:
        ser = None

    if serial_thread and serial_thread.is_alive():
        try:
            serial_thread.join(timeout=1.0)
        except Exception:
            pass
    serial_thread = None
    return True


# TRUYỀN socketio VÀO ĐÂY ĐỂ TRÁNH LỖI IMPORT VÒNG TRÒN
def start_serial_reader(socketio, port=None, baud=115200):
    global ser, serial_thread, stop_serial_thread

    if not SERIAL_ENABLED or pyserial is None: return False
    if not port: port = os.environ.get("SERIAL_PORT") or auto_detect_port()
    if not port: return False

    stop_serial_reader()

    try:
        ser = pyserial.Serial(port, baud, timeout=0.5)
        ser.reset_input_buffer()
    except Exception as e:
        print("Không mở được cổng serial:", e)
        return False

    stop_serial_thread = False
    last_angles = defaultdict(lambda: {"yaw": 0.0, "roll": 0.0, "pitch": 0.0, "ts": 0.0})

    def reader_loop():
        global stop_serial_thread
        while not stop_serial_thread:
            try:
                raw = ser.readline()
                if not raw: continue
                line = raw.decode("utf-8", errors="ignore").strip()
                parsed = parse_serial_line(line)
                if not parsed: continue

                ptype = parsed[0]
                now_ms = time.time() * 1000.0

                if ptype == "imu":
                    _, sid, ts, yaw, roll, pitch = parsed
                    last_angles[sid] = {"yaw": yaw, "roll": roll, "pitch": pitch, "ts": ts}

                    p1 = last_angles.get(1, {}).get("roll", 0.0)
                    p2 = last_angles.get(2, {}).get("roll", 0.0)
                    p3 = last_angles.get(3, {}).get("roll", 0.0)
                    p4 = -last_angles.get(4, {}).get("roll", 0.0)
                    pitch2 = last_angles.get(2, {}).get("pitch", 0.0)

                    raw_hip = norm_deg(p2 - p1)
                    raw_knee = norm_deg(p3 - p2)
                    raw_ankle = norm_deg(p4 - p3)

                    # Xử lý Logic Góc
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

                    hip = _smooth("hip", clamp(hip, -30.1, 122.1))
                    knee = _smooth("knee", clamp(abs(knee), 0, 134))
                    ankle = _smooth("ankle", clamp(abs(ankle), 36, 113))

                    with MAX_LOCK:
                        if hip > MAX_ANGLES["hip"]: MAX_ANGLES["hip"] = hip
                        if knee > MAX_ANGLES["knee"]: MAX_ANGLES["knee"] = knee
                        if ankle > MAX_ANGLES["ankle"]: MAX_ANGLES["ankle"] = ankle
                        max_payload = {
                            "maxHip": MAX_ANGLES["hip"],
                            "maxKnee": MAX_ANGLES["knee"],
                            "maxAnkle": MAX_ANGLES["ankle"],
                        }

                    with DATA_LOCK:
                        data_buffer.append({
                            "t_ms": now_ms, "hip": hip, "knee": knee, "ankle": ankle
                        })

                    # Bắn dữ liệu lên Web
                    socketio.emit("imu_data", {
                        "t": now_ms, "hip": hip, "knee": knee, "ankle": ankle, **max_payload
                    })

            except Exception as e:
                if "ClearCommError" in str(e) or "Access is denied" in str(e): break

    serial_thread = threading.Thread(target=reader_loop, daemon=True)
    serial_thread.start()
    return True