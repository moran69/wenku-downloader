#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度文库 VIP 下载器 - 网页版（多用户）。

启动: python app.py  （监听 0.0.0.0，端口由 WENKU_PORT 配置，默认 18900）
"""
import asyncio
import os
import re
import secrets
import sqlite3
import threading
import time
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
from werkzeug.security import check_password_hash, generate_password_hash

from downloader import (
    BASE_DIR,
    COOKIE_FILE,
    check_login_live,
    cookie_summary,
    download_document,
    load_cookie_text,
    parse_cookie_string,
    safe_filename,
)

DB_FILE = BASE_DIR / "data.db"
PASSWORD_FILE = BASE_DIR / "password.txt"
SECRET_FILE = BASE_DIR / ".secret_key"
USER_FILES_ROOT = BASE_DIR / "downloads"

PORT = int(os.environ.get("WENKU_PORT", "18900"))
MAX_CONCURRENT = int(os.environ.get("WENKU_MAX_CONCURRENT", "2"))
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("WENKU_MAX_PENDING", "10"))

app = Flask(__name__, template_folder="templates", static_folder="static")


def get_or_create_secret_key() -> str:
    """持久化 session 密钥，重启后已登录会话依然有效。"""
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


# ---------- 数据库 ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                url TEXT NOT NULL,
                status TEXT NOT NULL,
                started REAL NOT NULL,
                finished REAL,
                file TEXT,
                files TEXT,
                error TEXT,
                method TEXT,
                pages INTEGER,
                total_pages INTEGER,
                text_chars INTEGER,
                partial INTEGER,
                logs TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, started DESC);
            """
        )


def bootstrap_admin() -> None:
    """首次启动：创建管理员账号（沿用 password.txt 里的旧密码），迁移旧文件。"""
    with db() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        if n:
            return
        pw = ""
        if PASSWORD_FILE.exists():
            pw = PASSWORD_FILE.read_text(encoding="utf-8").strip()
        if not pw:
            pw = secrets.token_urlsafe(6)
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
            ("admin", generate_password_hash(pw), time.time()),
        )
        print(f"已创建初始管理员: admin / {pw}", flush=True)
        # 把旧版散落在 downloads/ 根目录的文件迁移到 admin 名下
        admin_dir = USER_FILES_ROOT / "admin"
        admin_dir.mkdir(parents=True, exist_ok=True)
        for f in USER_FILES_ROOT.iterdir():
            if f.is_file():
                try:
                    f.rename(admin_dir / f.name)
                except OSError:
                    pass


def user_dir(username: str) -> Path:
    d = USER_FILES_ROOT / username
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 认证 ----------
def current_user() -> dict | None:
    if not session.get("logged_in"):
        return None
    with db() as c:
        row = c.execute(
            "SELECT id, username, is_admin FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
    if not row:
        session.clear()
        return None
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        me = current_user()
        if not me:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login", next=request.path))
        request.me = me
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        me = getattr(request, "me", None) or current_user()
        if not me:
            return jsonify({"error": "未登录"}), 401
        if not me["is_admin"]:
            return jsonify({"error": "需要管理员权限"}), 403
        request.me = me
        return f(*args, **kwargs)

    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        with db() as c:
            row = c.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session["logged_in"] = True
            session["user_id"] = row["id"]
            nxt = request.args.get("next") or url_for("index")
            return redirect(nxt)
        error = "用户名或密码错误"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        me=request.me,
        is_admin=request.me["is_admin"],
    )


# ---------- 任务 ----------
def _job_log(job_id: str, logs: list[dict]) -> None:
    with db() as c:
        c.execute("UPDATE jobs SET logs = ? WHERE id = ?", (json_dumps(logs), job_id))


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def _run_download(job_id: str, url: str, out_dir: Path) -> None:
    logs: list[dict] = []

    def log_fn(m: str):
        logs.append({"t": time.time(), "m": m})
        try:
            _job_log(job_id, logs)
        except Exception:
            pass

    try:
        acquired = download_slots.acquire(timeout=0)
        if not acquired:
            log_fn(f"排队等待中（已有 {MAX_CONCURRENT} 个下载在运行）…")
            download_slots.acquire()
        try:
            result = asyncio.run(download_document(url, log=log_fn, output_dir=out_dir))
        finally:
            download_slots.release()
        with db() as c:
            c.execute(
                """UPDATE jobs SET status=?, finished=?, file=?, files=?, error=?,
                   method=?, pages=?, total_pages=?, text_chars=?, partial=?, logs=?
                   WHERE id=?""",
                (
                    "done" if result.get("success") else "failed",
                    time.time(),
                    result.get("file"),
                    json_dumps(result.get("files") or []),
                    result.get("error"),
                    result.get("method"),
                    result.get("pages"),
                    result.get("total_pages"),
                    result.get("text_chars"),
                    1 if result.get("partial") else 0,
                    json_dumps(logs),
                    job_id,
                ),
            )
    except Exception as e:
        with db() as c:
            c.execute(
                "UPDATE jobs SET status='failed', finished=?, error=?, logs=? WHERE id=?",
                (time.time(), f"内部错误: {e}", json_dumps(logs), job_id),
            )


@app.route("/api/submit", methods=["POST"])
@login_required
def submit():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or "wenku.baidu.com" not in url:
        return jsonify({"error": "请输入有效的百度文库链接（含 wenku.baidu.com）"}), 400
    me = request.me
    with db() as c:
        active = c.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE user_id=? AND status IN ('pending','downloading')",
            (me["id"],),
        ).fetchone()["n"]
        if active >= MAX_ACTIVE_JOBS_PER_USER:
            return jsonify({"error": f"排队任务过多（上限 {MAX_ACTIVE_JOBS_PER_USER}），请等当前任务完成"}), 429
        job_id = secrets.token_hex(6)
        c.execute(
            "INSERT INTO jobs (id, user_id, username, url, status, started, logs) VALUES (?,?,?,?,?,?,?)",
            (job_id, me["id"], me["username"], url, "downloading", time.time(), "[]"),
        )
    threading.Thread(target=_run_download, args=(job_id, url, user_dir(me["username"])), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs")
@login_required
def list_jobs():
    me = request.me
    show_all = me["is_admin"] and request.args.get("all") == "1"
    with db() as c:
        if show_all:
            rows = c.execute("SELECT * FROM jobs ORDER BY started DESC LIMIT 60").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM jobs WHERE user_id=? ORDER BY started DESC LIMIT 40",
                (me["id"],),
            ).fetchall()
    import json

    now = time.time()
    out = []
    for r in rows:
        started = r["started"]
        finished = r["finished"]
        out.append(
            {
                "id": r["id"],
                "url": r["url"],
                "username": r["username"],
                "status": r["status"],
                "started": started,
                "elapsed": round((finished or now) - started, 1),
                "file": r["file"],
                "files": json.loads(r["files"] or "[]"),
                "error": r["error"],
                "method": r["method"],
                "pages": r["pages"],
                "total_pages": r["total_pages"],
                "text_chars": r["text_chars"],
                "partial": bool(r["partial"]),
                "logs": json.loads(r["logs"] or "[]"),
            }
        )
    return jsonify(out)


# ---------- 文件 ----------
def _file_info(f: Path, owner: str) -> dict:
    st = f.stat()
    return {
        "name": f.name,
        "owner": owner,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "ext": f.suffix.lstrip(".").lower() or "file",
    }


@app.route("/api/files")
@login_required
def list_files():
    me = request.me
    show_all = me["is_admin"] and request.args.get("all") == "1"
    files = []
    if show_all:
        for d in sorted(USER_FILES_ROOT.iterdir()):
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f.is_file() and not f.name.startswith((".", "_")):
                    files.append(_file_info(f, d.name))
        files.sort(key=lambda x: x["mtime"], reverse=True)
    else:
        d = user_dir(me["username"])
        for f in d.iterdir():
            if f.is_file() and not f.name.startswith((".", "_")):
                files.append(_file_info(f, me["username"]))
        files.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(files)


@app.route("/api/download/<path:filename>")
@login_required
def download_file(filename: str):
    """支持两种形式：<文件名>（本人文件）或 <用户名>/<文件名>（管理员下载他人文件）。"""
    me = request.me
    parts = filename.split("/", 1)
    if len(parts) == 2:
        if not me["is_admin"]:
            abort(403)
        owner, name = parts[0], parts[1]
        if not re.fullmatch(r"[\w\u4e00-\u9fa5-]{1,32}", owner):
            abort(400)
    else:
        owner, name = me["username"], filename
    name = safe_filename(name)
    if ".." in name or ".." in owner:
        abort(400)
    base = USER_FILES_ROOT / owner
    if not (base / name).exists():
        abort(404)
    return send_from_directory(base, name, as_attachment=True)


@app.route("/api/delete-file/<path:filename>", methods=["DELETE"])
@login_required
def delete_file(filename: str):
    me = request.me
    parts = filename.split("/", 1)
    if len(parts) == 2:
        if not me["is_admin"]:
            abort(403)
        owner, name = parts[0], parts[1]
    else:
        owner, name = me["username"], filename
    name = safe_filename(name)
    if ".." in name or ".." in owner:
        abort(400)
    path = USER_FILES_ROOT / owner / name
    if not path.exists():
        return jsonify({"error": "文件不存在"}), 404
    path.unlink()
    return jsonify({"ok": True})


# ---------- 用户管理（管理员） ----------
USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fa5-]{2,24}$")


@app.route("/api/users")
@admin_required
def list_users():
    with db() as c:
        rows = c.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return jsonify(
        [
            {"username": r["username"], "is_admin": bool(r["is_admin"]), "created_at": r["created_at"]}
            for r in rows
        ]
    )


@app.route("/api/users", methods=["POST"])
@admin_required
def create_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    is_admin = 1 if data.get("is_admin") else 0
    if not USERNAME_RE.fullmatch(username):
        return jsonify({"error": "用户名需为 2-24 位字母/数字/汉字/下划线/中划线"}), 400
    if len(password) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error": "用户名已存在"}), 400
        c.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (username, generate_password_hash(password), is_admin, time.time()),
        )
    return jsonify({"ok": True})


@app.route("/api/users/<username>/password", methods=["POST"])
@admin_required
def reset_password(username: str):
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if len(password) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return jsonify({"error": "用户不存在"}), 404
        c.execute(
            "UPDATE users SET password_hash=? WHERE username=?",
            (generate_password_hash(password), username),
        )
    return jsonify({"ok": True})


@app.route("/api/users/<username>", methods=["DELETE"])
@admin_required
def delete_user(username: str):
    me = request.me
    if username == me["username"]:
        return jsonify({"error": "不能删除自己"}), 400
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not row:
            return jsonify({"error": "用户不存在"}), 404
        if row["is_admin"]:
            admins = c.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1").fetchone()["n"]
            if admins <= 1:
                return jsonify({"error": "不能删除最后一个管理员"}), 400
        c.execute("DELETE FROM users WHERE username=?", (username,))
        c.execute("DELETE FROM jobs WHERE user_id=?", (row["id"],))
    # 删除该用户的下载文件
    d = USER_FILES_ROOT / username
    if d.is_dir():
        for f in d.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
        d.rmdir()
    return jsonify({"ok": True})


# ---------- Cookie 管理（管理员） ----------
@app.route("/api/cookies", methods=["GET"])
@admin_required
def get_cookies():
    try:
        raw = load_cookie_text()
    except Exception as e:
        return jsonify({"raw": "", "summary": cookie_summary(), "error": str(e)})
    return jsonify({"raw": raw, "summary": cookie_summary()})


@app.route("/api/cookies", methods=["POST"])
@admin_required
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
@admin_required
def check_login():
    """联网验证当前 Cookie 是否有效（约 10 秒，会启动浏览器）。"""
    try:
        result = asyncio.run(check_login_live())
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "logged_in": False}), 500


# ---------- 启动 ----------
init_db()
bootstrap_admin()
# 服务重启后，把中断的任务标记为失败
with db() as c:
    c.execute(
        "UPDATE jobs SET status='failed', error='服务重启导致任务中断', finished=? WHERE status IN ('pending','downloading')",
        (time.time(),),
    )

if __name__ == "__main__":
    print(f"访问端口: {PORT}  并发下载: {MAX_CONCURRENT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
