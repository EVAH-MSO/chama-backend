"""
MiniChat backend — Full-featured Chama Edition (2 roles: treasurer + member)
Every confirmation goes through the treasurer only.
"""
import sqlite3
import os
import re
import uuid
import csv
import io
import secrets as _secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, request, jsonify, session, send_from_directory, Response
)
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ============ SETUP ============
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-in-production-xyz-123"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

UPLOAD_FOLDER = "uploads"
IMAGE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp",
    "heic", "heif", "jfif", "bmp", "svg", "avif",
}
VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "m4v", "avi", "mkv"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
MAX_FILE_SIZE = 50 * 1024 * 1024

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

# Allow both local dev + production frontend
ALLOWED_ORIGINS = [
    "http://localhost:5173",
]
# Add production URL if set
_env_frontend = os.environ.get("FRONTEND_URL", "").strip()
if _env_frontend:
    ALLOWED_ORIGINS.append(_env_frontend)

CORS(app, supports_credentials=True, origins=ALLOWED_ORIGINS)
socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_ORIGINS,
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)

DB_PATH = os.path.abspath("chat.db")

# ============ ROLES (SIMPLIFIED) ============
# Only 2 roles now
VALID_ROLES = {"treasurer", "member"}
# Treasurer does ALL confirmations
MANAGER_ROLES = {"treasurer"}


# ============ DATABASE ============

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            bio TEXT DEFAULT '',
            avatar_color TEXT DEFAULT '#667eea',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            user TEXT NOT NULL,
            text TEXT DEFAULT '',
            image_url TEXT,
            reply_to INTEGER,
            chama_id INTEGER,
            time TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user TEXT NOT NULL,
            emoji TEXT NOT NULL,
            UNIQUE(message_id, user_id, emoji),
            FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS statuses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT DEFAULT '',
            image_url TEXT,
            media_type TEXT,
            bg_color TEXT DEFAULT '#667eea',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS status_views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status_id INTEGER NOT NULL,
            viewer_id INTEGER NOT NULL,
            viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(status_id, viewer_id),
            FOREIGN KEY(status_id) REFERENCES statuses(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS chamas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            monthly_target REAL DEFAULT 1000,
            invite_code TEXT UNIQUE NOT NULL,
            logo_url TEXT,
            late_fine REAL DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chama_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT DEFAULT 'member',
            approved INTEGER DEFAULT 0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(chama_id, user_id),
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            month TEXT NOT NULL,
            paid_on TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            confirmed INTEGER DEFAULT 0,
            confirmed_by INTEGER,
            notes TEXT DEFAULT '',
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            interest_rate REAL DEFAULT 0,
            purpose TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            issued_at TIMESTAMP,
            due_date TEXT,
            repaid_at TIMESTAMP,
            approved_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS fines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            reason TEXT DEFAULT '',
            month TEXT,
            paid INTEGER DEFAULT 0,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS minutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            meeting_date TEXT NOT NULL,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chama_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            pinned INTEGER DEFAULT 1,
            created_by INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chama_id) REFERENCES chamas(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()
    print(f"✅ Database ready at {DB_PATH}")


# ============ HELPERS ============

def get_user_by_username(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(user_id)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return wrapper


def public_user(user):
    if not user:
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"] or user["username"],
        "bio": user["bio"] or "",
        "avatar_color": user["avatar_color"] or "#667eea",
    }


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def media_type_for(filename):
    if not filename:
        return None
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def is_chama_member(chama_id, user_id):
    if not chama_id or not user_id:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM chama_members WHERE chama_id = ? AND user_id = ?",
        (chama_id, user_id),
    ).fetchone()
    conn.close()
    if not row or not row["approved"]:
        return None
    return dict(row)


def is_treasurer(chama_id, user_id):
    """Only treasurer can do confirmations."""
    m = is_chama_member(chama_id, user_id)
    return m and m["role"] == "treasurer"


def can_manage(chama_id, user_id):
    """Alias for is_treasurer — kept for backward compat."""
    return is_treasurer(chama_id, user_id)


def can_access_room(chama_id, user_id):
    if chama_id is None:
        return True
    return is_chama_member(chama_id, user_id) is not None


def room_name(chama_id):
    return f"chama_{chama_id}" if chama_id else "global"


def gen_invite_code():
    return _secrets.token_hex(3).upper()


def get_reactions_for(conn, message_id):
    rows = conn.execute(
        "SELECT emoji, user_id, user FROM reactions WHERE message_id = ?",
        (message_id,),
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["emoji"], []).append({
            "user_id": r["user_id"],
            "user": r["user"],
        })
    return grouped


def get_reply_preview(conn, reply_to):
    if not reply_to:
        return None
    row = conn.execute(
        "SELECT user, text, image_url FROM messages WHERE id = ?",
        (reply_to,),
    ).fetchone()
    if not row:
        return None
    return {
        "user": row["user"],
        "text": (row["text"] or "")[:80] or ("📷 Media" if row["image_url"] else ""),
    }


def save_message(user_id, user, text="", image_url=None, reply_to=None,
                 chama_id=None, time=None):
    if time is None:
        time = datetime.now().strftime("%H:%M")
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO messages (user_id, user, text, image_url, reply_to, chama_id, time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, user, text, image_url, reply_to, chama_id, time),
    )
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id


def get_recent_messages(chama_id=None, limit=200):
    conn = get_db()
    if chama_id:
        rows = conn.execute(
            "SELECT id, user_id, user, text, image_url, reply_to, chama_id, time "
            "FROM messages WHERE chama_id = ? ORDER BY id ASC LIMIT ?",
            (chama_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, user_id, user, text, image_url, reply_to, chama_id, time "
            "FROM messages WHERE chama_id IS NULL ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    messages = []
    for row in rows:
        msg = dict(row)
        msg["reply_preview"] = get_reply_preview(conn, msg.get("reply_to"))
        msg["reactions"] = get_reactions_for(conn, msg["id"])
        messages.append(msg)
    conn.close()
    return messages


init_db()


# ============ AUTH ============

@app.route("/api/auth/signup", methods=["POST"])
def signup():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not re.match(r"^[a-z0-9_]{3,20}$", username):
        return jsonify({"error": "Username must be 3-20 chars: letters, numbers, underscore only"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if get_user_by_username(username):
        return jsonify({"error": "Username already taken"}), 409

    conn = get_db()
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name) VALUES (?, ?, ?)",
        (username, generate_password_hash(password), username),
    )
    conn.commit()
    conn.close()

    user = get_user_by_username(username)
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    user = get_user_by_username(username)
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Invalid username or password"}), 401

    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify({"user": public_user(user)})


@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "ok"})


@app.route("/api/auth/me")
def me():
    user = get_current_user()
    if not user:
        return jsonify({"user": None}), 200
    return jsonify({"user": public_user(user)})


@app.route("/api/users/me", methods=["PATCH"])
@login_required
def update_me():
    data = request.get_json() or {}
    user = get_current_user()
    display_name = (data.get("display_name") or user["display_name"]).strip()[:40]
    bio = (data.get("bio") or user["bio"]).strip()[:200]
    avatar_color = data.get("avatar_color") or user["avatar_color"]

    conn = get_db()
    conn.execute(
        "UPDATE users SET display_name = ?, bio = ?, avatar_color = ? WHERE id = ?",
        (display_name, bio, avatar_color, user["id"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"user": public_user(get_user_by_id(user["id"]))})


# ============ CHAT (scoped by chama) ============

@app.route("/api/messages", methods=["GET"])
def api_messages():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Not authenticated"}), 401

    chama_id = request.args.get("chama_id", type=int)
    if not can_access_room(chama_id, user["id"]):
        return jsonify({"error": "Not a member of this chama"}), 403

    return jsonify(get_recent_messages(chama_id=chama_id))


@app.route("/api/messages/<int:message_id>", methods=["DELETE"])
@login_required
def delete_message(message_id):
    user = get_current_user()
    conn = get_db()
    row = conn.execute(
        "SELECT user_id, chama_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Message not found"}), 404
    if row["user_id"] != user["id"]:
        conn.close()
        return jsonify({"error": "Not your message"}), 403

    chama_id = row["chama_id"]
    conn.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    conn.commit()
    conn.close()
    socketio.emit("message_deleted", {"id": message_id}, to=room_name(chama_id))
    return jsonify({"status": "deleted", "id": message_id})


@app.route("/api/messages/<int:message_id>", methods=["PATCH"])
@login_required
def edit_message(message_id):
    user = get_current_user()
    data = request.get_json() or {}
    new_text = (data.get("text") or "").strip()
    if not new_text:
        return jsonify({"error": "Empty text"}), 400

    conn = get_db()
    row = conn.execute(
        "SELECT user_id, chama_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Message not found"}), 404
    if row["user_id"] != user["id"]:
        conn.close()
        return jsonify({"error": "Not your message"}), 403

    chama_id = row["chama_id"]
    conn.execute("UPDATE messages SET text = ? WHERE id = ?", (new_text, message_id))
    conn.commit()
    conn.close()

    payload = {"id": message_id, "text": new_text}
    socketio.emit("message_edited", payload, to=room_name(chama_id))
    return jsonify({"status": "updated", **payload})


@app.route("/api/messages/clear", methods=["POST"])
@login_required
def clear_all_messages():
    user = get_current_user()
    chama_id = request.args.get("chama_id", type=int)
    if not can_access_room(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    conn = get_db()
    if chama_id:
        conn.execute(
            "DELETE FROM reactions WHERE message_id IN "
            "(SELECT id FROM messages WHERE chama_id = ?)", (chama_id,)
        )
        conn.execute("DELETE FROM messages WHERE chama_id = ?", (chama_id,))
    else:
        conn.execute(
            "DELETE FROM reactions WHERE message_id IN "
            "(SELECT id FROM messages WHERE chama_id IS NULL)"
        )
        conn.execute("DELETE FROM messages WHERE chama_id IS NULL")
    conn.commit()
    conn.close()

    socketio.emit("chat_cleared", {"by": user["display_name"], "chama_id": chama_id},
                  to=room_name(chama_id))
    return jsonify({"status": "cleared"})


@app.route("/api/messages/<int:message_id>/react", methods=["POST"])
@login_required
def toggle_reaction(message_id):
    user = get_current_user()
    data = request.get_json() or {}
    emoji = (data.get("emoji") or "").strip()[:8]
    if not emoji:
        return jsonify({"error": "Empty emoji"}), 400

    conn = get_db()
    msg = conn.execute(
        "SELECT id, chama_id FROM messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not msg:
        conn.close()
        return jsonify({"error": "Message not found"}), 404

    if not can_access_room(msg["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Not a member"}), 403

    existing = conn.execute(
        "SELECT id FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
        (message_id, user["id"], emoji),
    ).fetchone()

    if existing:
        conn.execute("DELETE FROM reactions WHERE id = ?", (existing["id"],))
        action = "removed"
    else:
        display = user["display_name"] or user["username"]
        conn.execute(
            "INSERT INTO reactions (message_id, user_id, user, emoji) VALUES (?, ?, ?, ?)",
            (message_id, user["id"], display, emoji),
        )
        action = "added"

    updated = get_reactions_for(conn, message_id)
    conn.commit()
    conn.close()

    socketio.emit("reactions_updated", {"message_id": message_id, "reactions": updated},
                  to=room_name(msg["chama_id"]))
    return jsonify({"status": action, "message_id": message_id, "emoji": emoji,
                    "reactions": updated})


# ============ UPLOAD ============

@app.route("/api/upload", methods=["POST"])
@login_required
def upload_image():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    file = request.files["file"]
    if not file.filename or "." not in file.filename:
        return jsonify({"error": "Invalid filename"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"File type '.{ext}' not supported"}), 400

    chama_id = request.form.get("chama_id")
    if chama_id:
        try:
            chama_id = int(chama_id)
        except (ValueError, TypeError):
            chama_id = None

    user = get_current_user()
    if not can_access_room(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    text = (request.form.get("text") or "").strip()
    display = user["display_name"] or user["username"]
    time_str = datetime.now().strftime("%H:%M")
    image_url = f"/api/uploads/{filename}"

    reply_to = request.form.get("reply_to")
    if reply_to:
        try:
            reply_to = int(reply_to)
        except (ValueError, TypeError):
            reply_to = None

    reply_preview = None
    if reply_to:
        conn = get_db()
        parent = conn.execute(
            "SELECT id FROM messages WHERE id = ?", (reply_to,)
        ).fetchone()
        if not parent:
            reply_to = None
        else:
            reply_preview = get_reply_preview(conn, reply_to)
        conn.close()

    msg_id = save_message(user["id"], display, text, image_url, reply_to, chama_id, time_str)
    msg = {
        "id": msg_id,
        "user_id": user["id"],
        "user": display,
        "text": text,
        "image_url": image_url,
        "reply_to": reply_to,
        "reply_preview": reply_preview,
        "chama_id": chama_id,
        "reactions": {},
        "time": time_str,
    }
    socketio.emit("new_message", msg, to=room_name(chama_id))
    return jsonify({"message": msg}), 201


@app.route("/api/uploads/<filename>")
def serve_upload(filename):
    safe = secure_filename(filename)
    if not safe or safe != filename:
        return jsonify({"error": "Invalid filename"}), 400
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ============ STATUS ============

def cleanup_expired_statuses():
    conn = get_db()
    conn.execute("DELETE FROM statuses WHERE expires_at < datetime('now')")
    conn.commit()
    conn.close()


def status_views_count(status_id):
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM status_views WHERE status_id = ?", (status_id,)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def has_viewed(status_id, viewer_id):
    if not viewer_id:
        return False
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM status_views WHERE status_id = ? AND viewer_id = ?",
        (status_id, viewer_id),
    ).fetchone()
    conn.close()
    return bool(row)


@app.route("/api/status", methods=["POST"])
@login_required
def create_status():
    user = get_current_user()
    text = (request.form.get("text") or "").strip()[:200]
    bg_color = request.form.get("bg_color") or "#667eea"
    image_url = None
    media_type = None

    if "file" in request.files:
        file = request.files["file"]
        if file and file.filename and "." in file.filename:
            ext = file.filename.rsplit(".", 1)[1].lower()
            if ext in ALLOWED_EXTENSIONS:
                filename = f"status_{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
                file.save(filepath)
                image_url = f"/api/uploads/{filename}"
                media_type = "video" if ext in VIDEO_EXTENSIONS else "image"

    if not text and not image_url:
        return jsonify({"error": "Status needs text, image, or video"}), 400

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO statuses (user_id, text, image_url, media_type, bg_color, expires_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now', '+24 hours'))",
        (user["id"], text, image_url, media_type, bg_color),
    )
    status_id = cursor.lastrowid
    conn.commit()
    conn.close()

    payload = {
        "id": status_id,
        "user_id": user["id"],
        "user": user["display_name"] or user["username"],
        "avatar_color": user["avatar_color"],
        "text": text,
        "image_url": image_url,
        "media_type": media_type,
        "bg_color": bg_color,
        "created_at": datetime.now().isoformat(),
    }
    socketio.emit("status_created", payload)
    return jsonify({"status": payload}), 201


@app.route("/api/status/feed", methods=["GET"])
def status_feed():
    cleanup_expired_statuses()
    me = get_current_user()
    my_id = me["id"] if me else 0

    conn = get_db()
    rows = conn.execute(
        """SELECT s.id, s.user_id, s.text, s.image_url, s.media_type, s.bg_color,
                  s.created_at, u.username, u.display_name, u.avatar_color
           FROM statuses s JOIN users u ON u.id = s.user_id
           WHERE s.expires_at > datetime('now')
           ORDER BY s.created_at ASC"""
    ).fetchall()

    by_user = {}
    for row in rows:
        uid = row["user_id"]
        if uid not in by_user:
            by_user[uid] = {
                "user_id": uid,
                "username": row["username"],
                "display_name": row["display_name"] or row["username"],
                "avatar_color": row["avatar_color"],
                "is_me": uid == my_id,
                "statuses": [],
                "all_viewed": True,
            }
        viewed = has_viewed(row["id"], my_id) if me else False
        if not viewed and uid != my_id:
            by_user[uid]["all_viewed"] = False
        media_type = row["media_type"] or media_type_for(row["image_url"])
        by_user[uid]["statuses"].append({
            "id": row["id"],
            "text": row["text"],
            "image_url": row["image_url"],
            "media_type": media_type,
            "bg_color": row["bg_color"],
            "created_at": row["created_at"],
            "viewed": viewed,
            "views_count": status_views_count(row["id"]),
        })

    conn.close()
    feed = sorted(by_user.values(), key=lambda u: (not u["is_me"], u["all_viewed"]))
    return jsonify(feed)


@app.route("/api/status/mine", methods=["GET"])
@login_required
def my_statuses():
    user = get_current_user()
    conn = get_db()
    rows = conn.execute(
        """SELECT id, text, image_url, media_type, bg_color, created_at, expires_at
           FROM statuses WHERE user_id = ? AND expires_at > datetime('now')
           ORDER BY created_at DESC""",
        (user["id"],),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        media_type = r["media_type"] or media_type_for(r["image_url"])
        result.append({
            "id": r["id"], "text": r["text"], "image_url": r["image_url"],
            "media_type": media_type, "bg_color": r["bg_color"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "views_count": status_views_count(r["id"]),
        })
    return jsonify(result)


@app.route("/api/status/<int:status_id>/view", methods=["POST"])
@login_required
def mark_status_viewed(status_id):
    user = get_current_user()
    conn = get_db()
    owner = conn.execute(
        "SELECT user_id FROM statuses WHERE id = ?", (status_id,)
    ).fetchone()
    if not owner:
        conn.close()
        return jsonify({"error": "Status not found"}), 404
    if owner["user_id"] != user["id"]:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO status_views (status_id, viewer_id) VALUES (?, ?)",
                (status_id, user["id"]),
            )
            conn.commit()
        except Exception:
            pass
    conn.close()
    return jsonify({"status": "ok"})


@app.route("/api/status/<int:status_id>", methods=["DELETE"])
@login_required
def delete_status(status_id):
    user = get_current_user()
    conn = get_db()
    row = conn.execute(
        "SELECT user_id FROM statuses WHERE id = ?", (status_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Status not found"}), 404
    if row["user_id"] != user["id"]:
        conn.close()
        return jsonify({"error": "Not your status"}), 403
    conn.execute("DELETE FROM statuses WHERE id = ?", (status_id,))
    conn.commit()
    conn.close()
    socketio.emit("status_deleted", {"id": status_id})
    return jsonify({"status": "deleted"})


# ============ CHAMA CORE ============

def get_user_chamas(user_id):
    conn = get_db()
    rows = conn.execute(
        """SELECT c.id, c.name, c.description, c.monthly_target,
                  c.invite_code, c.logo_url, c.late_fine, c.created_by,
                  m.role, m.approved
           FROM chamas c
           JOIN chama_members m ON m.chama_id = c.id
           WHERE m.user_id = ? AND m.approved = 1
           ORDER BY c.created_at DESC""",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.route("/api/chamas", methods=["POST"])
@login_required
def create_chama():
    if request.content_type and "multipart" in request.content_type:
        name = (request.form.get("name") or "").strip()[:60]
        description = (request.form.get("description") or "").strip()[:200]
        try:
            target = float(request.form.get("monthly_target") or 1000)
        except (ValueError, TypeError):
            target = 1000
        try:
            late_fine = float(request.form.get("late_fine") or 0)
        except (ValueError, TypeError):
            late_fine = 0
        logo_file = request.files.get("logo")
    else:
        data = request.get_json() or {}
        name = (data.get("name") or "").strip()[:60]
        description = (data.get("description") or "").strip()[:200]
        try:
            target = float(data.get("monthly_target") or 1000)
        except (ValueError, TypeError):
            target = 1000
        try:
            late_fine = float(data.get("late_fine") or 0)
        except (ValueError, TypeError):
            late_fine = 0
        logo_file = None

    target = max(10, min(target, 10_000_000))
    late_fine = max(0, min(late_fine, 1_000_000))

    if len(name) < 3:
        return jsonify({"error": "Chama name must be at least 3 characters"}), 400

    logo_url = None
    if logo_file and logo_file.filename and allowed_file(logo_file.filename):
        ext = logo_file.filename.rsplit(".", 1)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            filename = f"chama_logo_{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
            logo_file.save(filepath)
            logo_url = f"/api/uploads/{filename}"

    code = gen_invite_code()
    user = get_current_user()
    conn = get_db()
    while conn.execute("SELECT id FROM chamas WHERE invite_code = ?", (code,)).fetchone():
        code = gen_invite_code()

    cursor = conn.execute(
        "INSERT INTO chamas (name, description, monthly_target, invite_code, logo_url, late_fine, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, description, target, code, logo_url, late_fine, user["id"]),
    )
    chama_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO chama_members (chama_id, user_id, role, approved) VALUES (?, ?, ?, 1)",
        (chama_id, user["id"], "treasurer"),
    )
    conn.commit()
    conn.close()

    return jsonify({"chama": {
        "id": chama_id, "name": name, "description": description,
        "monthly_target": target, "invite_code": code,
        "logo_url": logo_url, "late_fine": late_fine, "role": "treasurer",
    }}), 201


@app.route("/api/chamas/join", methods=["POST"])
@login_required
def join_chama():
    data = request.get_json() or {}
    code = (data.get("invite_code") or "").strip().upper()
    if not code:
        return jsonify({"error": "Invite code required"}), 400

    user = get_current_user()
    conn = get_db()
    chama = conn.execute("SELECT * FROM chamas WHERE invite_code = ?", (code,)).fetchone()
    if not chama:
        conn.close()
        return jsonify({"error": "Invalid invite code"}), 404

    existing = conn.execute(
        "SELECT id, approved FROM chama_members WHERE chama_id = ? AND user_id = ?",
        (chama["id"], user["id"]),
    ).fetchone()
    if existing:
        conn.close()
        if existing["approved"]:
            return jsonify({"error": "You're already a member"}), 409
        return jsonify({"error": "Your request is pending approval"}), 409

    conn.execute(
        "INSERT INTO chama_members (chama_id, user_id, role, approved) "
        "VALUES (?, ?, 'member', 0)",
        (chama["id"], user["id"]),
    )
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama["id"]})
    return jsonify({
        "status": "pending",
        "message": "Join request sent. Treasurer will approve soon.",
    }), 202


@app.route("/api/chamas/mine", methods=["GET"])
@login_required
def my_chamas():
    user = get_current_user()
    return jsonify(get_user_chamas(user["id"]))


@app.route("/api/chamas/<int:chama_id>", methods=["GET"])
@login_required
def chama_detail(chama_id):
    user = get_current_user()
    membership = is_chama_member(chama_id, user["id"])
    if not membership:
        return jsonify({"error": "Not a member"}), 403

    month = request.args.get("month") or datetime.now().strftime("%Y-%m")

    conn = get_db()
    chama = conn.execute("SELECT * FROM chamas WHERE id = ?", (chama_id,)).fetchone()
    if not chama:
        conn.close()
        return jsonify({"error": "Chama not found"}), 404

    members = conn.execute(
        """SELECT u.id, u.username, u.display_name, u.avatar_color,
                  m.role, m.joined_at
           FROM chama_members m
           JOIN users u ON u.id = m.user_id
           WHERE m.chama_id = ? AND m.approved = 1
           ORDER BY CASE m.role
                      WHEN 'treasurer' THEN 1
                      ELSE 2 END,
                    u.display_name ASC""",
        (chama_id,),
    ).fetchall()

    contribs = conn.execute(
        """SELECT c.id, c.user_id, c.amount, c.month, c.paid_on,
                  c.confirmed, c.confirmed_by, c.notes
           FROM contributions c
           WHERE c.chama_id = ? AND c.month = ?
           ORDER BY c.paid_on ASC""",
        (chama_id, month),
    ).fetchall()

    contrib_by_user = {c["user_id"]: dict(c) for c in contribs}

    anns = conn.execute(
        """SELECT a.id, a.text, a.pinned, a.created_at, u.display_name, u.username
           FROM announcements a
           LEFT JOIN users u ON u.id = a.created_by
           WHERE a.chama_id = ?
           ORDER BY a.created_at DESC LIMIT 5""",
        (chama_id,),
    ).fetchall()

    member_list = []
    paid_count = 0
    total_paid = 0.0
    for m in members:
        m_dict = dict(m)
        m_dict["contribution"] = contrib_by_user.get(m["id"])
        if m_dict["contribution"]:
            paid_count += 1
            total_paid += m_dict["contribution"]["amount"]
        member_list.append(m_dict)

    conn.close()

    return jsonify({
        "chama": dict(chama),
        "month": month,
        "members": member_list,
        "announcements": [dict(a) for a in anns],
        "stats": {
            "total_members": len(member_list),
            "paid_count": paid_count,
            "total_paid": round(total_paid, 2),
            "target_total": round(chama["monthly_target"] * len(member_list), 2),
            "my_role": membership["role"],
        },
    })


@app.route("/api/chamas/<int:chama_id>/contribute", methods=["POST"])
@login_required
def log_contribution(chama_id):
    user = get_current_user()
    membership = is_chama_member(chama_id, user["id"])
    if not membership:
        return jsonify({"error": "Not a member"}), 403

    data = request.get_json() or {}
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    month = (data.get("month") or datetime.now().strftime("%Y-%m")).strip()
    notes = (data.get("notes") or "").strip()[:200]

    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM contributions WHERE chama_id = ? AND user_id = ? AND month = ?",
        (chama_id, user["id"], month),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE contributions SET amount = ?, notes = ?, paid_on = CURRENT_TIMESTAMP, confirmed = 0 "
            "WHERE id = ?",
            (amount, notes, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO contributions (chama_id, user_id, amount, month, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (chama_id, user["id"], amount, month, notes),
        )
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "message": "Contribution logged"})


# ============ TREASURER CONFIRMATIONS ============

@app.route("/api/contributions/<int:contrib_id>/confirm", methods=["POST"])
@login_required
def confirm_contribution(contrib_id):
    """ONLY treasurer can confirm contributions."""
    user = get_current_user()
    conn = get_db()
    contrib = conn.execute(
        "SELECT chama_id FROM contributions WHERE id = ?", (contrib_id,)
    ).fetchone()
    if not contrib:
        conn.close()
        return jsonify({"error": "Contribution not found"}), 404

    if not is_treasurer(contrib["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can confirm contributions"}), 403

    conn.execute(
        "UPDATE contributions SET confirmed = 1, confirmed_by = ? WHERE id = ?",
        (user["id"], contrib_id),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": contrib["chama_id"]})
    return jsonify({"status": "confirmed"})


@app.route("/api/contributions/<int:contrib_id>/unconfirm", methods=["POST"])
@login_required
def unconfirm_contribution(contrib_id):
    """ONLY treasurer can un-confirm if a mistake was made."""
    user = get_current_user()
    conn = get_db()
    contrib = conn.execute(
        "SELECT chama_id FROM contributions WHERE id = ?", (contrib_id,)
    ).fetchone()
    if not contrib:
        conn.close()
        return jsonify({"error": "Contribution not found"}), 404

    if not is_treasurer(contrib["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can do this"}), 403

    conn.execute(
        "UPDATE contributions SET confirmed = 0, confirmed_by = NULL WHERE id = ?",
        (contrib_id,),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": contrib["chama_id"]})
    return jsonify({"status": "unconfirmed"})


# ============ HISTORICAL MONTHS ============

@app.route("/api/chamas/<int:chama_id>/history", methods=["GET"])
@login_required
def chama_history(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    conn = get_db()
    rows = conn.execute(
        """SELECT month,
                  COUNT(*) as contributors,
                  SUM(amount) as total,
                  SUM(CASE WHEN confirmed = 1 THEN 1 ELSE 0 END) as confirmed_count
           FROM contributions
           WHERE chama_id = ?
           GROUP BY month
           ORDER BY month DESC""",
        (chama_id,),
    ).fetchall()

    total_members = conn.execute(
        "SELECT COUNT(*) as c FROM chama_members WHERE chama_id = ? AND approved = 1",
        (chama_id,),
    ).fetchone()["c"]
    conn.close()

    history = []
    for r in rows:
        history.append({
            "month": r["month"],
            "contributors": r["contributors"],
            "total_members": total_members,
            "total": round(r["total"] or 0, 2),
            "confirmed_count": r["confirmed_count"],
            "completion_pct": round(100 * r["contributors"] / total_members, 1) if total_members else 0,
        })
    return jsonify(history)


# ============ CSV EXPORT ============

@app.route("/api/chamas/<int:chama_id>/export.csv", methods=["GET"])
@login_required
def export_csv(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    month = request.args.get("month") or datetime.now().strftime("%Y-%m")

    conn = get_db()
    chama = conn.execute("SELECT name FROM chamas WHERE id = ?", (chama_id,)).fetchone()
    if not chama:
        conn.close()
        return jsonify({"error": "Chama not found"}), 404

    rows = conn.execute(
        """SELECT u.username, u.display_name, c.amount, c.month, c.paid_on,
                  c.confirmed, c.notes
           FROM contributions c
           JOIN users u ON u.id = c.user_id
           WHERE c.chama_id = ? AND c.month = ?
           ORDER BY u.display_name ASC""",
        (chama_id, month),
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Chama", chama["name"]])
    writer.writerow(["Month", month])
    writer.writerow([])
    writer.writerow(["Username", "Display Name", "Amount (KES)", "Month",
                     "Paid On", "Confirmed", "Notes"])
    for r in rows:
        writer.writerow([
            r["username"], r["display_name"], r["amount"], r["month"],
            r["paid_on"], "Yes" if r["confirmed"] else "No", r["notes"],
        ])

    filename = f"{chama['name'].replace(' ', '_')}_{month}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ============ LOANS (treasurer approves) ============

@app.route("/api/chamas/<int:chama_id>/loans", methods=["GET", "POST"])
@login_required
def chama_loans(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    if request.method == "GET":
        conn = get_db()
        rows = conn.execute(
            """SELECT l.*, u.username, u.display_name, u.avatar_color
               FROM loans l
               JOIN users u ON u.id = l.user_id
               WHERE l.chama_id = ?
               ORDER BY l.created_at DESC""",
            (chama_id,),
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    data = request.get_json() or {}
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    try:
        interest_rate = float(data.get("interest_rate") or 0)
    except (ValueError, TypeError):
        interest_rate = 0

    purpose = (data.get("purpose") or "").strip()[:200]
    due_date = (data.get("due_date") or "").strip()

    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO loans (chama_id, user_id, amount, interest_rate, purpose, due_date)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chama_id, user["id"], amount, interest_rate, purpose, due_date),
    )
    loan_id = cursor.lastrowid
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "loan_id": loan_id}), 201


@app.route("/api/loans/<int:loan_id>/approve", methods=["POST"])
@login_required
def approve_loan(loan_id):
    """ONLY treasurer approves loans."""
    user = get_current_user()
    conn = get_db()
    loan = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if not is_treasurer(loan["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can approve loans"}), 403

    conn.execute(
        "UPDATE loans SET status = 'active', issued_at = CURRENT_TIMESTAMP, approved_by = ? WHERE id = ?",
        (user["id"], loan_id),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": loan["chama_id"]})
    return jsonify({"status": "approved"})


@app.route("/api/loans/<int:loan_id>/reject", methods=["POST"])
@login_required
def reject_loan(loan_id):
    """ONLY treasurer rejects loans."""
    user = get_current_user()
    conn = get_db()
    loan = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if not is_treasurer(loan["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can reject loans"}), 403

    conn.execute("UPDATE loans SET status = 'rejected' WHERE id = ?", (loan_id,))
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": loan["chama_id"]})
    return jsonify({"status": "rejected"})


@app.route("/api/loans/<int:loan_id>/repay", methods=["POST"])
@login_required
def repay_loan(loan_id):
    """Borrower OR treasurer can mark repaid."""
    user = get_current_user()
    conn = get_db()
    loan = conn.execute("SELECT * FROM loans WHERE id = ?", (loan_id,)).fetchone()
    if not loan:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    if loan["user_id"] != user["id"] and not is_treasurer(loan["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the borrower or treasurer"}), 403

    conn.execute(
        "UPDATE loans SET status = 'repaid', repaid_at = CURRENT_TIMESTAMP WHERE id = ?",
        (loan_id,),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": loan["chama_id"]})
    return jsonify({"status": "repaid"})


# ============ FINES (treasurer only) ============

@app.route("/api/chamas/<int:chama_id>/fines", methods=["GET", "POST"])
@login_required
def chama_fines(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    if request.method == "GET":
        conn = get_db()
        rows = conn.execute(
            """SELECT f.*, u.username, u.display_name, u.avatar_color
               FROM fines f
               JOIN users u ON u.id = f.user_id
               WHERE f.chama_id = ?
               ORDER BY f.created_at DESC""",
            (chama_id,),
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    # POST — ONLY treasurer creates fines
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can create fines"}), 403

    data = request.get_json() or {}
    try:
        amount = float(data.get("amount") or 0)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount"}), 400
    if amount <= 0:
        return jsonify({"error": "Amount must be positive"}), 400

    target_user_id = data.get("user_id")
    if not target_user_id:
        return jsonify({"error": "User required"}), 400
    if not is_chama_member(chama_id, target_user_id):
        return jsonify({"error": "Target user is not a member"}), 400

    reason = (data.get("reason") or "").strip()[:200]
    month = (data.get("month") or datetime.now().strftime("%Y-%m")).strip()

    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO fines (chama_id, user_id, amount, reason, month, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (chama_id, target_user_id, amount, reason, month, user["id"]),
    )
    fine_id = cursor.lastrowid
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "fine_id": fine_id}), 201


@app.route("/api/fines/<int:fine_id>/mark-paid", methods=["POST"])
@login_required
def mark_fine_paid(fine_id):
    """ONLY treasurer marks fines paid."""
    user = get_current_user()
    conn = get_db()
    fine = conn.execute("SELECT * FROM fines WHERE id = ?", (fine_id,)).fetchone()
    if not fine:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if not is_treasurer(fine["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can mark fines paid"}), 403

    conn.execute("UPDATE fines SET paid = 1 WHERE id = ?", (fine_id,))
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": fine["chama_id"]})
    return jsonify({"status": "paid"})


@app.route("/api/chamas/<int:chama_id>/auto-fine", methods=["POST"])
@login_required
def auto_fine(chama_id):
    """ONLY treasurer auto-fines unpaid members."""
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can auto-fine"}), 403

    data = request.get_json() or {}
    month = (data.get("month") or datetime.now().strftime("%Y-%m")).strip()

    conn = get_db()
    chama = conn.execute("SELECT late_fine FROM chamas WHERE id = ?", (chama_id,)).fetchone()
    if not chama:
        conn.close()
        return jsonify({"error": "Chama not found"}), 404
    if not chama["late_fine"] or chama["late_fine"] <= 0:
        conn.close()
        return jsonify({"error": "Set a late_fine amount on the chama first"}), 400

    unpaid = conn.execute(
        """SELECT u.id FROM chama_members m
           JOIN users u ON u.id = m.user_id
           WHERE m.chama_id = ? AND m.approved = 1
             AND u.id NOT IN (
                 SELECT user_id FROM contributions
                 WHERE chama_id = ? AND month = ?
             )""",
        (chama_id, chama_id, month),
    ).fetchall()

    created = 0
    for row in unpaid:
        existing = conn.execute(
            "SELECT id FROM fines WHERE chama_id = ? AND user_id = ? AND month = ?",
            (chama_id, row["id"], month),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """INSERT INTO fines (chama_id, user_id, amount, reason, month, created_by)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (chama_id, row["id"], chama["late_fine"],
             f"Late contribution for {month}", month, user["id"]),
        )
        created += 1

    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "fines_created": created})


# ============ MINUTES (treasurer only) ============

@app.route("/api/chamas/<int:chama_id>/minutes", methods=["GET", "POST"])
@login_required
def chama_minutes(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    if request.method == "GET":
        conn = get_db()
        rows = conn.execute(
            """SELECT m.*, u.display_name, u.username
               FROM minutes m
               LEFT JOIN users u ON u.id = m.created_by
               WHERE m.chama_id = ?
               ORDER BY m.meeting_date DESC""",
            (chama_id,),
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    # POST — ONLY treasurer
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can add minutes"}), 403

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()[:200]
    content = (data.get("content") or "").strip()[:5000]
    meeting_date = (data.get("meeting_date") or datetime.now().strftime("%Y-%m-%d")).strip()

    if not title or not content:
        return jsonify({"error": "Title and content required"}), 400

    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO minutes (chama_id, title, content, meeting_date, created_by)
           VALUES (?, ?, ?, ?, ?)""",
        (chama_id, title, content, meeting_date, user["id"]),
    )
    minute_id = cursor.lastrowid
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "id": minute_id}), 201


@app.route("/api/minutes/<int:minute_id>", methods=["DELETE"])
@login_required
def delete_minute(minute_id):
    """ONLY treasurer deletes minutes."""
    user = get_current_user()
    conn = get_db()
    m = conn.execute("SELECT * FROM minutes WHERE id = ?", (minute_id,)).fetchone()
    if not m:
        conn.close()
        return jsonify({"error": "Not found"}), 404

    if not is_treasurer(m["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can delete minutes"}), 403

    conn.execute("DELETE FROM minutes WHERE id = ?", (minute_id,))
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": m["chama_id"]})
    return jsonify({"status": "deleted"})


# ============ ANNOUNCEMENTS (treasurer only) ============

@app.route("/api/chamas/<int:chama_id>/announcements", methods=["GET", "POST"])
@login_required
def chama_announcements(chama_id):
    user = get_current_user()
    if not is_chama_member(chama_id, user["id"]):
        return jsonify({"error": "Not a member"}), 403

    if request.method == "GET":
        conn = get_db()
        rows = conn.execute(
            """SELECT a.*, u.display_name, u.username
               FROM announcements a
               LEFT JOIN users u ON u.id = a.created_by
               WHERE a.chama_id = ?
               ORDER BY a.pinned DESC, a.created_at DESC
               LIMIT 20""",
            (chama_id,),
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    # POST — ONLY treasurer
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can post announcements"}), 403

    data = request.get_json() or {}
    text = (data.get("text") or "").strip()[:500]
    if not text:
        return jsonify({"error": "Text required"}), 400

    pinned = 1 if data.get("pinned", True) else 0

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO announcements (chama_id, text, pinned, created_by) VALUES (?, ?, ?, ?)",
        (chama_id, text, pinned, user["id"]),
    )
    ann_id = cursor.lastrowid
    conn.commit()
    conn.close()

    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "id": ann_id}), 201


@app.route("/api/announcements/<int:ann_id>", methods=["DELETE"])
@login_required
def delete_announcement(ann_id):
    """ONLY treasurer deletes announcements."""
    user = get_current_user()
    conn = get_db()
    a = conn.execute("SELECT * FROM announcements WHERE id = ?", (ann_id,)).fetchone()
    if not a:
        conn.close()
        return jsonify({"error": "Not found"}), 404
    if not is_treasurer(a["chama_id"], user["id"]):
        conn.close()
        return jsonify({"error": "Only the treasurer can delete announcements"}), 403

    conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": a["chama_id"]})
    return jsonify({"status": "deleted"})


# ============ ROLES (treasurer manages) ============

@app.route("/api/chamas/<int:chama_id>/members/<int:user_id>/role", methods=["PATCH"])
@login_required
def update_member_role(chama_id, user_id):
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can change roles"}), 403

    data = request.get_json() or {}
    new_role = (data.get("role") or "").strip().lower()
    if new_role not in VALID_ROLES:
        return jsonify({"error": "Role must be 'treasurer' or 'member'"}), 400

    # Prevent demoting last treasurer
    if new_role != "treasurer":
        conn = get_db()
        current = conn.execute(
            "SELECT role FROM chama_members WHERE chama_id = ? AND user_id = ?",
            (chama_id, user_id),
        ).fetchone()
        if current and current["role"] == "treasurer":
            treasurers = conn.execute(
                "SELECT COUNT(*) as c FROM chama_members WHERE chama_id = ? AND role = 'treasurer' AND approved = 1",
                (chama_id,),
            ).fetchone()["c"]
            if treasurers <= 1:
                conn.close()
                return jsonify({"error": "Cannot demote the only treasurer. Promote another member first."}), 400
        conn.close()

    conn = get_db()
    conn.execute(
        "UPDATE chama_members SET role = ? WHERE chama_id = ? AND user_id = ?",
        (new_role, chama_id, user_id),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "ok", "role": new_role})


# ============ PENDING / APPROVE / REJECT (treasurer only) ============

@app.route("/api/chamas/<int:chama_id>/pending", methods=["GET"])
@login_required
def chama_pending(chama_id):
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can view pending"}), 403

    conn = get_db()
    rows = conn.execute(
        """SELECT m.id as membership_id, u.id, u.username, u.display_name,
                  u.avatar_color, m.joined_at
           FROM chama_members m
           JOIN users u ON u.id = m.user_id
           WHERE m.chama_id = ? AND m.approved = 0
           ORDER BY m.joined_at ASC""",
        (chama_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/chamas/<int:chama_id>/members/<int:membership_id>/approve", methods=["POST"])
@login_required
def approve_member(chama_id, membership_id):
    """ONLY treasurer approves members."""
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can approve members"}), 403

    conn = get_db()
    conn.execute(
        "UPDATE chama_members SET approved = 1 WHERE id = ? AND chama_id = ?",
        (membership_id, chama_id),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "approved"})


@app.route("/api/chamas/<int:chama_id>/members/<int:membership_id>", methods=["DELETE"])
@login_required
def reject_member(chama_id, membership_id):
    """ONLY treasurer rejects members."""
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can reject members"}), 403

    conn = get_db()
    conn.execute(
        "DELETE FROM chama_members WHERE id = ? AND chama_id = ?",
        (membership_id, chama_id),
    )
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"status": "rejected"})


# ============ LOGO + INVITE CODE (treasurer only) ============

@app.route("/api/chamas/<int:chama_id>/logo", methods=["POST"])
@login_required
def upload_chama_logo(chama_id):
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can change the logo"}), 403

    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    file = request.files["file"]
    if not file.filename or "." not in file.filename:
        return jsonify({"error": "Invalid file"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return jsonify({"error": "Only images"}), 400

    filename = f"chama_logo_{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    logo_url = f"/api/uploads/{filename}"

    conn = get_db()
    conn.execute("UPDATE chamas SET logo_url = ? WHERE id = ?", (logo_url, chama_id))
    conn.commit()
    conn.close()
    socketio.emit("chama_updated", {"chama_id": chama_id})
    return jsonify({"logo_url": logo_url})


@app.route("/api/chamas/<int:chama_id>/regenerate-code", methods=["POST"])
@login_required
def regenerate_invite(chama_id):
    user = get_current_user()
    if not is_treasurer(chama_id, user["id"]):
        return jsonify({"error": "Only the treasurer can regenerate code"}), 403

    conn = get_db()
    new_code = gen_invite_code()
    while conn.execute("SELECT id FROM chamas WHERE invite_code = ?", (new_code,)).fetchone():
        new_code = gen_invite_code()
    conn.execute("UPDATE chamas SET invite_code = ? WHERE id = ?", (new_code, chama_id))
    conn.commit()
    conn.close()
    return jsonify({"invite_code": new_code})


# ============ HEALTH ============

@app.route("/")
def root():
    return {"status": "ok", "service": "chatbot-backend"}


@app.route("/health")
def health():
    return {"status": "healthy"}


# ============ SOCKET.IO ============

connected_users = {}


def join_user_rooms(user):
    join_room("global")
    conn = get_db()
    rows = conn.execute(
        "SELECT chama_id FROM chama_members WHERE user_id = ? AND approved = 1",
        (user["id"],),
    ).fetchall()
    conn.close()
    for r in rows:
        join_room(f"chama_{r['chama_id']}")
    return len(rows)


@socketio.on("connect")
def on_connect(*args, **kwargs):
    user = get_current_user()
    print(f"🔌 Socket connected — user: {user['username'] if user else 'ANON'}")
    connected_users[request.sid] = user["username"] if user else "Anonymous"
    if user:
        join_user_rooms(user)
    emit("user_count", {"count": len(connected_users)}, broadcast=True)


@socketio.on("disconnect")
def on_disconnect():
    connected_users.pop(request.sid, None)
    emit("user_count", {"count": len(connected_users)}, broadcast=True)


@socketio.on("register")
def on_register(data):
    username = (data.get("username") or "Anonymous").strip()
    connected_users[request.sid] = username
    user = get_current_user()
    if user:
        join_user_rooms(user)
    emit("user_count", {"count": len(connected_users)}, broadcast=True)


@socketio.on("typing")
def on_typing(data):
    chama_id = data.get("chama_id")
    user = get_current_user()
    if not can_access_room(chama_id, user["id"] if user else None):
        return
    emit("user_typing", {
        "user": data.get("user"),
        "is_typing": data.get("is_typing", False),
        "chama_id": chama_id,
    }, to=room_name(chama_id), skip_sid=request.sid)


@socketio.on("send_message")
def on_send_message(data):
    user = get_current_user()
    if not user:
        return

    chama_id = data.get("chama_id")
    if chama_id:
        try:
            chama_id = int(chama_id)
        except (ValueError, TypeError):
            chama_id = None

    if not can_access_room(chama_id, user["id"]):
        return

    text = (data.get("text") or "").strip()
    image_url = data.get("image_url")
    reply_to = data.get("reply_to")
    if not text and not image_url:
        return

    time = datetime.now().strftime("%H:%M")
    display = user["display_name"] or user["username"]

    reply_preview = None
    if reply_to:
        conn = get_db()
        parent = conn.execute("SELECT id FROM messages WHERE id = ?", (reply_to,)).fetchone()
        if not parent:
            reply_to = None
        else:
            reply_preview = get_reply_preview(conn, reply_to)
        conn.close()

    msg_id = save_message(user["id"], display, text, image_url, reply_to, chama_id, time)
    payload = {
        "id": msg_id, "user_id": user["id"], "user": display,
        "text": text, "image_url": image_url,
        "reply_to": reply_to, "reply_preview": reply_preview,
        "chama_id": chama_id, "reactions": {}, "time": time,
    }
    emit("new_message", payload, to=room_name(chama_id))


# ============ START ============

if __name__ == "__main__":
    print("🚀 MiniChat backend starting...")
    print(f"   DB: {DB_PATH}")
    print("   Roles: treasurer | member (treasurer does ALL confirmations)")
    print("   → http://localhost:5000")
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=True,
        allow_unsafe_werkzeug=True,
    )