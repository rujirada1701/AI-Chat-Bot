from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import timedelta
from collections import Counter, defaultdict
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.utils import secure_filename

from rag_chat_local import answer_question, initialize_rag_components, warmup_runtime


BASE_DIR = Path(__file__).resolve().parent
MANUALS_DIR = BASE_DIR / "manuals"
ALLOWED_MANUAL_EXTENSIONS = {".txt", ".docx"}
app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)

UPLOAD_USERNAME = os.getenv("UPLOAD_USERNAME", "admin")
UPLOAD_PASSWORD = os.getenv("UPLOAD_PASSWORD", "sat2026")

_runtime_lock = threading.Lock()
_runtime: dict[str, object] | None = None
_upload_status_lock = threading.Lock()
_upload_status: dict[str, object] = {
    "state": "idle",
    "message": "พร้อมใช้งาน",
    "saved_files": [],
    "skipped_files": [],
    "loaded_files": [],
}


def _warmup_runtime_in_background() -> None:
    """รัน warmup แบบกัน exception เพื่อไม่ให้ thread พ่น traceback"""
    try:
        warmup_runtime(get_runtime())
    except Exception:
        pass


def _set_upload_status(**updates: object) -> None:
    with _upload_status_lock:
        _upload_status.update(updates)


def _get_upload_status() -> dict[str, object]:
    with _upload_status_lock:
        status = dict(_upload_status)

    for key in ("saved_files", "skipped_files", "loaded_files"):
        value = status.get(key)
        status[key] = list(value) if isinstance(value, list) else []
    return status


def _get_loaded_files_snapshot() -> list[str]:
    runtime = _runtime
    if runtime is None:
        return []
    return [file_path.name for file_path in runtime.get("manual_files", [])]


def _refresh_runtime_after_upload(saved_files: list[str], skipped_files: list[str]) -> None:
    """อัปเดตฐานความรู้หลังอัปโหลดแบบ background เพื่อไม่บล็อก request"""
    global _runtime

    _set_upload_status(
        state="processing",
        message="กำลังอัปเดตฐานความรู้จากไฟล์ที่อัปโหลด",
        saved_files=saved_files,
        skipped_files=skipped_files,
    )

    try:
        new_runtime = initialize_rag_components()
        with _runtime_lock:
            _runtime = new_runtime

        loaded_files = [file_path.name for file_path in new_runtime.get("manual_files", [])]
        _set_upload_status(
            state="ready",
            message="อัปโหลดและอัปเดตฐานความรู้สำเร็จ",
            saved_files=saved_files,
            skipped_files=skipped_files,
            loaded_files=loaded_files,
        )

        thread = threading.Thread(target=_warmup_runtime_in_background, daemon=True)
        thread.start()
    except Exception as exc:
        _set_upload_status(
            state="error",
            message=f"อัปเดตฐานความรู้ไม่สำเร็จ: {exc}",
            saved_files=saved_files,
            skipped_files=skipped_files,
            loaded_files=_get_loaded_files_snapshot(),
        )


def _start_warmup_thread() -> None:
    """อุ่น runtime ล่วงหน้าโดยไม่บล็อกการเปิดหน้าเว็บ"""
    thread = threading.Thread(target=_warmup_runtime_in_background, daemon=True)
    thread.start()


def get_runtime() -> dict[str, object]:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                _runtime = initialize_rag_components()
    return _runtime


def _is_allowed_manual_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_MANUAL_EXTENSIONS


def _upload_login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if not session.get("upload_authenticated"):
            return jsonify({"message": "กรุณาเข้าสู่ระบบก่อนอัปโหลดไฟล์"}), 401
        return view(*args, **kwargs)

    return wrapped_view


_start_warmup_thread()


@app.get("/")
@app.get("/qa")
def qa_page():
    return send_from_directory(BASE_DIR, "qa.html")


@app.get("/upload-status")
def upload_status():
    return jsonify(_get_upload_status())


@app.get("/api/upload-auth")
def upload_auth_status():
    return jsonify({"authenticated": bool(session.get("upload_authenticated"))})


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))

    if username != UPLOAD_USERNAME or password != UPLOAD_PASSWORD:
        return jsonify({"message": "Username หรือ Password ไม่ถูกต้อง"}), 401

    session.clear()
    session.permanent = True
    session["upload_authenticated"] = True
    return jsonify({"authenticated": True})


@app.post("/api/logout")
def logout():
    session.pop("upload_authenticated", None)
    return jsonify({"authenticated": False})


@app.post("/ask")
def ask():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    machine = str(payload.get("machine", "")).strip()
    symptom = str(payload.get("symptom", "")).strip()

    if not question:
        if machine and symptom:
            question = f"MACHINE: {machine}\nอาการ: {symptom}"
        elif machine:
            question = machine
        elif symptom:
            question = symptom

    if not question:
        return jsonify({"answer": "กรุณากรอกคำถาม"}), 400

    try:
        runtime = get_runtime()
        answer = answer_question(
            question=question,
            retriever=runtime["retriever"],
            llm=runtime["llm"],
            prompt=runtime["prompt"],
            known_records=runtime["known_records"],
            query_source="web",
        )
    except Exception as exc:
        answer = (
            "MACHINE: ไม่พบข้อมูล\n"
            "อาการ: ไม่พบข้อมูล\n"
            "สาเหตุ: ไม่พบข้อมูล\n"
            f"การแก้ไข: เกิดข้อผิดพลาดในการประมวลผล ({exc})"
        )

    return jsonify({"answer": answer})


@app.post("/upload-manuals")
@_upload_login_required
def upload_manuals():
    uploaded_files = request.files.getlist("files")
    if not uploaded_files:
        return jsonify({"message": "ไม่พบไฟล์ที่อัปโหลด"}), 400

    MANUALS_DIR.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []
    skipped_files: list[str] = []

    for file_storage in uploaded_files:
        raw_name = file_storage.filename or ""
        safe_name = secure_filename(raw_name)

        if not safe_name:
            continue

        if not _is_allowed_manual_file(safe_name):
            skipped_files.append(raw_name)
            continue

        save_path = MANUALS_DIR / safe_name
        file_storage.save(save_path)
        saved_files.append(safe_name)

    if not saved_files:
        return jsonify({"message": "ไฟล์ไม่ถูกต้อง รองรับเฉพาะ .txt และ .docx", "skipped_files": skipped_files}), 400

    _set_upload_status(
        state="queued",
        message="รับไฟล์แล้ว กำลังรออัปเดตฐานความรู้",
        saved_files=saved_files,
        skipped_files=skipped_files,
        loaded_files=_get_loaded_files_snapshot(),
    )

    thread = threading.Thread(
        target=_refresh_runtime_after_upload,
        args=(saved_files.copy(), skipped_files.copy()),
        daemon=True,
    )
    thread.start()

    return jsonify(
        {
            "message": "รับไฟล์แล้ว กำลังอัปเดตฐานความรู้ในพื้นหลัง",
            "state": "queued",
            "saved_files": saved_files,
            "skipped_files": skipped_files,
            "loaded_files": _get_loaded_files_snapshot(),
        }
    ), 202


@app.get("/dashboard")
def dashboard_page():
    return send_from_directory(BASE_DIR, "dashboard.html")


def _parse_logs(start_date: str = None, end_date: str = None) -> dict[str, object]:
    """Parse log files and return statistics, optionally filtered by date range"""
    logs_dir = BASE_DIR / "logs"
    all_logs = []
    
    # Parse JSONL file
    jsonl_file = logs_dir / "question_logs.jsonl"
    if jsonl_file.exists():
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        all_logs.append(json.loads(line))
        except Exception:
            pass
    
    # Parse CSV files from subdirectories
    for csv_file in logs_dir.glob("*/question_logs.csv"):
        if csv_file.exists():
            try:
                with open(csv_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    if len(lines) > 1:  # Skip header
                        headers = lines[0].strip().split(",")
                        for line in lines[1:]:
                            parts = line.strip().split(",", len(headers)-1)
                            if len(parts) == len(headers):
                                log_entry = dict(zip(headers, parts))
                                all_logs.append(log_entry)
            except Exception:
                pass
    
    # Filter by date range if provided
    if start_date or end_date:
        filtered_logs = []
        for log in all_logs:
            log_date = log.get("date", "")
            if start_date and end_date:
                if start_date <= log_date <= end_date:
                    filtered_logs.append(log)
            elif start_date:
                if log_date >= start_date:
                    filtered_logs.append(log)
            elif end_date:
                if log_date <= end_date:
                    filtered_logs.append(log)
        all_logs = filtered_logs
    
    # Calculate statistics
    stats = {
        "total_questions": len(all_logs),
        "date_range": {},
        "by_source": {},
        "by_hour": {},
        "top_questions": [],
        "questions_by_date": {},
    }
    
    if all_logs:
        # Date range
        dates = sorted([log.get("date", "") for log in all_logs if log.get("date")])
        if dates:
            stats["date_range"] = {
                "start": dates[0],
                "end": dates[-1]
            }
        
        # Questions by source
        sources = Counter(log.get("source", "unknown") for log in all_logs)
        stats["by_source"] = dict(sources)
        
        # Questions by hour
        hourly = defaultdict(int)
        for log in all_logs:
            time_str = log.get("time", "")
            if time_str:
                try:
                    hour = time_str.split(":")[0]
                    hourly[hour] += 1
                except Exception:
                    pass
        stats["by_hour"] = dict(sorted(hourly.items()))
        
        # Top questions
        questions = Counter(log.get("question", "") for log in all_logs if log.get("question"))
        top_10 = questions.most_common(10)
        stats["top_questions"] = [{"question": q, "count": c} for q, c in top_10]
        
        # Questions by date
        daily = defaultdict(int)
        for log in all_logs:
            date = log.get("date", "")
            if date:
                daily[date] += 1
        stats["questions_by_date"] = dict(sorted(daily.items()))
    
    return stats


@app.get("/api/logs-statistics")
def logs_statistics():
    """Return log statistics for dashboard with optional date filtering"""
    start_date = request.args.get("start_date", None)
    end_date = request.args.get("end_date", None)
    
    stats = _parse_logs(start_date=start_date, end_date=end_date)
    return jsonify(stats)


@app.get("/api/available-dates")
def available_dates():
    """Return all available dates in logs for date picker"""
    logs_dir = BASE_DIR / "logs"
    all_dates = set()
    
    # Parse JSONL file
    jsonl_file = logs_dir / "question_logs.jsonl"
    if jsonl_file.exists():
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        if "date" in data:
                            all_dates.add(data["date"])
        except Exception:
            pass
    
    # Parse CSV files
    for csv_file in logs_dir.glob("*/question_logs.csv"):
        if csv_file.exists():
            try:
                with open(csv_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    if len(lines) > 1:
                        for line in lines[1:]:
                            parts = line.strip().split(",")
                            if len(parts) >= 2:
                                all_dates.add(parts[1])
            except Exception:
                pass
    
    sorted_dates = sorted(list(all_dates))
    
    # Get unique months
    months = set()
    for date in sorted_dates:
        try:
            month = date[:7]  # YYYY-MM format
            months.add(month)
        except Exception:
            pass
    
    return jsonify({
        "dates": sorted_dates,
        "months": sorted(list(months))
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
