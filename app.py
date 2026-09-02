#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度文库 VIP 下载器 - 网页版。

启动: python app.py  （监听 0.0.0.0:8000）
"""
import asyncio
import os
import secrets
import threading
import time
import urllib.parse
from collections import deque
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from downloader import (
    COOKIE_FILE,
    KEY_COOKIE_NAMES,
    OUTPUT_DIR,
    check_login_live,
    cookie_summary,
    download_document,
    load_cookie_text,
    parse_cookie_string,
    safe_filename,
)

BASE_DIR = Path(__file__).resolve().parent
PASSWORD_FILE = BASE_DIR / "password.txt"
SECRET_FILE = BASE_DIR / ".secret_key"

# 可用环境变量覆盖：WENKU_PORT / WENKU_MAX_CONCURRENT
PORT = int(os.environ.get("WENKU_PORT", "18900"))
MAX_CONCURRENT = int(os.environ.get("WENKU_MAX_CONCURRENT", "2"))

app = Flask(__name__, template_folder="templates", static_folder="static")


def get_or_create_secret_key() -> str:
    """持久化 session 密钥，重启后已登录的会话依然有效。"""
    if SECRET_FILE.exists():
        key = SECRET_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_hex(24)
    SECRET_FILE.write_text(key, encoding="utf-8")
    try:
        SECRET_FILE.chmod(0o600)
    except OSError:
        pass
    return key


app.secret_key = get_or_create_secret_key()

# 并发下载槽：防止同时启动过多 Chromium 把内存打爆
download_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


# ---------- 鉴权 ----------
def get_or_create_password() -> str:
    if PASSWORD_FILE.exists():
        pw = PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if pw:
            return pw
    pw = secrets.token_urlsafe(6)
    PASSWORD_FILE.write_text(pw, encoding="utf-8")
    return pw


PASSWORD = get_or_create_password()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == PASSWORD:
            session["logged_in"] = True
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        error = "密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ---------- 任务管理 ----------
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
RECENT_MAX = 80
recent_ids = deque(maxlen=RECENT_MAX)


def _run_download(job_id: str, url: str) -> None:
    logs: list[dict] = []

    def log_fn(m: str):
        logs.append({"t": time.time(), "m": m})
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["logs"] = list(logs)

    try:
        acquired = download_slots.acquire(timeout=0)
        if not acquired:
            log_fn(f"排队等待中（已有 {MAX_CONCURRENT} 个下载在运行）…")
            download_slots.acquire()
        try:
            result = asyncio.run(download_document(url, log=log_fn))
        finally:
            download_slots.release()
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(result)
                jobs[job_id]["status"] = "done" if result.get("success") else "failed"
                jobs[job_id]["finished"] = time.time()
    except Exception as e:
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["status"] = "failed"
                jobs[job_id]["error"] = f"内部错误: {e}"
                jobs[job_id]["finished"] = time.time()


@app.route("/api/submit", methods=["POST"])
@login_required
def submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or "wenku.baidu.com" not in url:
        return jsonify({"error": "请输入有效的百度文库链接（含 wenku.baidu.com）"}), 400
    job_id = secrets.token_hex(6)
    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "url": url,
            "status": "pending",
            "started": now,
            "finished": None,
            "file": None,
            "error": None,
            "logs": [],
        }
        recent_ids.append(job_id)
    threading.Thread(target=_run_download, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs")
@login_required
def list_jobs():
    with jobs_lock:
        ids = list(recent_ids)[::-1]
        out = [dict(jobs[i]) for i in ids if i in jobs]
    for j in out:
        if j.get("started"):
            j["elapsed"] = round((j.get("finished") or time.time()) - j["started"], 1)
    return jsonify(out)


# ---------- 文件 ----------
@app.route("/api/files")
@login_required
def list_files():
    files = []
    for f in sorted(OUTPUT_DIR.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.name.startswith(".") or f.name.startswith("_"):
            continue
        if f.is_file():
            st = f.stat()
            files.append(
                {
                    "name": f.name,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "ext": f.suffix.lstrip(".").lower() or "file",
                }
            )
    return jsonify(files)


@app.route("/api/download/<path:filename>")
@login_required
def download_file(filename: str):
    name = safe_filename(filename)
    path = OUTPUT_DIR / name
    if not path.exists() or ".." in name:
        abort(404)
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True)


@app.route("/api/delete-file/<path:filename>", methods=["DELETE"])
@login_required
def delete_file(filename: str):
    name = safe_filename(filename)
    path = OUTPUT_DIR / name
    if ".." in name:
        abort(400)
    if not path.exists():
        return jsonify({"error": "文件不存在"}), 404
    path.unlink()
    return jsonify({"ok": True})


# ---------- Cookie 管理 ----------
@app.route("/api/cookies", methods=["GET"])
@login_required
def get_cookies():
    try:
        raw = load_cookie_text()
    except Exception as e:
        return jsonify({"raw": "", "summary": cookie_summary(), "error": str(e)})
    return jsonify({"raw": raw, "summary": cookie_summary()})


@app.route("/api/cookies", methods=["POST"])
@login_required
def update_cookies():
    data = request.get_json(silent=True) or {}
    raw = (data.get("cookies") or "").strip()
    if not raw:
        return jsonify({"error": "Cookie 内容不能为空"}), 400
    parsed = parse_cookie_string(raw)
    if "BDUSS" not in {c["name"] for c in parsed}:
        return jsonify({"error": "Cookie 中缺少 BDUSS，可能不是完整的登录 Cookie，请重新复制"}), 400
    COOKIE_FILE.write_text(raw, encoding="utf-8")
    return jsonify({"ok": True, "summary": cookie_summary()})


@app.route("/api/check-login", methods=["POST"])
@login_required
def check_login():
    """联网验证当前 Cookie 是否有效（耗时约 10 秒，会启动浏览器）。"""
    try:
        result = asyncio.run(check_login_live())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "logged_in": False}), 500


@app.context_processor
def inject_vars():
    return {"PASSWORD_LOCATION": str(PASSWORD_FILE)}


if __name__ == "__main__":
    print(f"访问密码: {PASSWORD}  (存放于 {PASSWORD_FILE})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
