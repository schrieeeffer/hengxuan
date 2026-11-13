from __future__ import annotations

import base64
import csv
import functools
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.exceptions import MethodNotAllowed, NotFound
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from help_content import HELP_TOPICS, HELP_TOPICS_BY_SLUG

USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff-]{2,20}$", re.UNICODE)
ROOM_CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")
ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_STATUS_LABELS = {
    "draft": "准备中",
    "scoring": "评分中",
    "closed": "已结束",
}
SCHEME_PRESETS = {
    "5": (1, 5),
    "10": (1, 10),
    "100": (0, 100),
}
SCHEMA_VERSION = 4
INVITE_EXPIRY_DAYS = {"1": 1, "7": 7, "30": 30}
RESULT_VISIBILITY_LABELS = {
    "live": "评分中可见",
    "after_complete": "完成本人评分后可见",
    "after_closed": "评分结束后可见",
}
AUDIT_ACTION_LABELS = {
    "room.created": "创建房间",
    "room.updated": "更新房间设置",
    "room.status_changed": "切换评分状态",
    "room.owner_transferred": "转让房主",
    "member.added": "添加成员",
    "member.joined": "加入房间",
    "member.updated": "更新成员权限",
    "member.removed": "移出成员",
    "member.restored": "恢复成员",
    "member.scores_included": "恢复计入成员评分",
    "member.scores_excluded": "作废成员评分",
    "invite.created": "创建邀请",
    "invite.revoked": "撤销邀请",
    "candidate.created": "添加候选人",
    "candidate.updated": "更新候选人",
    "candidate.archived": "归档候选人",
    "candidate.restored": "恢复候选人",
    "score.saved": "保存评分",
    "score.removed": "撤销评分",
    "account.recovery_generated": "生成账号恢复码",
    "account.password_reset": "重置账号密码",
    "account.password_changed": "修改账号密码",
    "account.sessions_revoked": "退出其他设备",
    "account.admin_reset_issued": "生成管理员重置码",
}

_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMIT_EVENTS: dict[str, list[float]] = {}


USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    session_version INTEGER NOT NULL DEFAULT 1 CHECK (session_version >= 1),
    recovery_code_hash TEXT,
    recovery_issued_at TEXT,
    is_platform_admin INTEGER NOT NULL DEFAULT 0
        CHECK (is_platform_admin IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

ROOMS_SQL = """
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    join_password_hash TEXT NOT NULL,
    owner_id INTEGER,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'scoring', 'closed')),
    score_min INTEGER NOT NULL DEFAULT 1 CHECK (score_min BETWEEN 0 AND 999),
    score_max INTEGER NOT NULL DEFAULT 5 CHECK (score_max BETWEEN 1 AND 1000),
    members_locked INTEGER NOT NULL DEFAULT 0 CHECK (members_locked IN (0, 1)),
    join_policy TEXT NOT NULL DEFAULT 'password'
        CHECK (join_policy IN ('password', 'invite_only')),
    result_visibility TEXT NOT NULL DEFAULT 'after_complete'
        CHECK (result_visibility IN ('live', 'after_complete', 'after_closed')),
    roster_revision INTEGER NOT NULL DEFAULT 1 CHECK (roster_revision >= 1),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL,
    CHECK (score_max > score_min)
)
"""

ROOM_MEMBERS_SQL = """
CREATE TABLE IF NOT EXISTS room_members (
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'judge' CHECK (role IN ('admin', 'judge')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    can_score INTEGER NOT NULL DEFAULT 1 CHECK (can_score IN (0, 1)),
    scores_included INTEGER NOT NULL DEFAULT 1
        CHECK (scores_included IN (0, 1)),
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    removed_at TEXT,
    removed_by INTEGER,
    removal_reason TEXT,
    PRIMARY KEY (room_id, user_id),
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (removed_by) REFERENCES users(id) ON DELETE SET NULL
)
"""

CANDIDATES_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    candidate_no TEXT,
    name TEXT NOT NULL,
    group_name TEXT,
    notes TEXT,
    position INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, room_id),
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE
)
"""

SCORES_SQL = """
CREATE TABLE IF NOT EXISTS scores (
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 1000),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (room_id, user_id, candidate_id),
    FOREIGN KEY (room_id, user_id)
        REFERENCES room_members(room_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id, room_id)
        REFERENCES candidates(id, room_id) ON DELETE CASCADE
)
"""

REVIEW_ASSIGNMENTS_SQL = """
CREATE TABLE IF NOT EXISTS review_assignments (
    room_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'required'
        CHECK (status IN ('required', 'completed', 'waived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (room_id, user_id, candidate_id),
    FOREIGN KEY (room_id, user_id)
        REFERENCES room_members(room_id, user_id) ON DELETE CASCADE,
    FOREIGN KEY (candidate_id, room_id)
        REFERENCES candidates(id, room_id) ON DELETE CASCADE
)
"""

ROOM_INVITATIONS_SQL = """
CREATE TABLE IF NOT EXISTS room_invitations (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'judge' CHECK (role IN ('admin', 'judge')),
    can_score INTEGER NOT NULL DEFAULT 1 CHECK (can_score IN (0, 1)),
    assign_existing INTEGER NOT NULL DEFAULT 1
        CHECK (assign_existing IN (0, 1)),
    max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses BETWEEN 1 AND 100),
    used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
)
"""

PASSWORD_RESET_TOKENS_SQL = """
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE SET NULL
)
"""

AUDIT_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    room_id INTEGER,
    actor_user_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
)
"""

USER_NOTIFICATIONS_SQL = """
CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    room_id INTEGER,
    room_name TEXT NOT NULL,
    actor_user_id INTEGER,
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (room_id) REFERENCES rooms(id) ON DELETE SET NULL,
    FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE SET NULL
)
"""

SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

V3_INDEX_STATEMENTS = (
    (
        "CREATE INDEX IF NOT EXISTS idx_room_members_user_active "
        "ON room_members(user_id, active)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_candidates_room_active_position "
        "ON candidates(room_id, archived, position)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_scores_room_candidate "
        "ON scores(room_id, candidate_id)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_assignments_room_candidate_status "
        "ON review_assignments(room_id, candidate_id, status)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_assignments_room_user_status "
        "ON review_assignments(room_id, user_id, status)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_invitations_room_active "
        "ON room_invitations(room_id, revoked_at, expires_at)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_reset_tokens_user_active "
        "ON password_reset_tokens(user_id, used_at, expires_at)"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_audit_room_created "
        "ON audit_logs(room_id, created_at DESC)"
    ),
)

INDEX_STATEMENTS = V3_INDEX_STATEMENTS + (
    (
        "CREATE INDEX IF NOT EXISTS idx_notifications_user_created "
        "ON user_notifications(user_id, created_at DESC, id DESC)"
    ),
)


def _load_or_create_secret(data_dir: Path) -> str:
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured

    data_dir.mkdir(parents=True, exist_ok=True)
    secret_path = data_dir / ".secret_key"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except FileNotFoundError:
        pass

    secret = secrets.token_hex(32)
    try:
        with secret_path.open("x", encoding="utf-8") as secret_file:
            secret_file.write(secret)
        secret_path.chmod(0o600)
        return secret
    except FileExistsError:
        return secret_path.read_text(encoding="utf-8").strip()


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    configured_database = os.environ.get(
        "DATABASE_PATH", str(Path(__file__).resolve().parent / "data" / "scores.db")
    )
    if test_config and "DATABASE" in test_config:
        configured_database = str(test_config["DATABASE"])
    data_dir = Path(configured_database).parent

    app.config.from_mapping(
        DATABASE=configured_database,
        SECRET_KEY=_load_or_create_secret(data_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_NAME="collaborative_scoring_session",
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        # Platform recovery operators are bound to already-provisioned numeric IDs.
        # Public registration must never grant global privileges by a claimable name.
        PLATFORM_ADMIN_USER_IDS={
            int(value.strip())
            for value in os.environ.get("PLATFORM_ADMIN_USER_IDS", "").split(",")
            if value.strip().isdigit() and int(value.strip()) > 0
        },
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    init_db(app)

    @app.before_request
    def load_logged_in_user() -> None:
        user_id = session.get("user_id")
        if user_id is None:
            g.user = None
            return
        user = get_db().execute(
            """
            SELECT id, username, session_version, recovery_code_hash,
                   recovery_issued_at, is_platform_admin, created_at
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if user is None or session.get("session_version") != user["session_version"]:
            session.clear()
            g.user = None
            return
        g.user = user

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.context_processor
    def inject_template_values() -> dict[str, Any]:
        def help_url(slug: str | None = None, anchor: str | None = None) -> str:
            if request.endpoint in {"help_center", "help_topic"}:
                return_path = safe_internal_return_path(
                    request.args.get("return_to")
                ) or url_for("rooms")
            else:
                return_path = request.full_path.removesuffix("?")
            endpoint = "help_topic" if slug else "help_center"
            values: dict[str, Any] = {"return_to": return_path}
            if slug:
                values["slug"] = slug
            if anchor:
                values["_anchor"] = anchor
            return url_for(endpoint, **values)

        return {
            "csrf_token": get_csrf_token,
            "room_status_labels": ROOM_STATUS_LABELS,
            "result_visibility_labels": RESULT_VISIBILITY_LABELS,
            "audit_action_labels": AUDIT_ACTION_LABELS,
            "is_platform_admin": is_platform_admin,
            "help_url": help_url,
        }

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(410)
    @app.errorhandler(429)
    @app.errorhandler(500)
    def friendly_error(error):
        code = getattr(error, "code", 500) or 500
        messages = {
            400: "请求已过期或内容不完整，请返回上一页后重试。",
            403: "你没有执行此操作的权限。",
            404: "没有找到该页面，或你的房间权限已发生变化。",
            410: "该链接已经失效。",
            429: "操作过于频繁，请稍后再试。",
            500: "服务暂时无法完成请求，请稍后重试。",
        }
        if code == 400 and getattr(error, "description", None):
            message = str(error.description)
        else:
            message = messages.get(code, messages[500])
        return render_template("error.html", error_code=code, message=message), code

    @app.teardown_appcontext
    def close_database(_error: BaseException | None) -> None:
        database = g.pop("db", None)
        if database is not None:
            database.close()

    @app.get("/")
    def index():
        if g.user is None:
            return render_template("index.html")
        return redirect(url_for("rooms"))

    @app.get("/favicon.ico")
    def favicon_compat():
        return redirect(url_for("static", filename="favicon.svg"), code=308)

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if g.user is not None:
            return redirect(url_for("rooms"))

        if request.method == "POST":
            require_valid_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            error = validate_account(username, password, confirm_password)
            database = get_db()
            if error is None:
                try:
                    database.execute("BEGIN IMMEDIATE")
                    pending_invite = session.get("pending_invite_token")
                    recovery_code = generate_recovery_code()
                    cursor = database.execute(
                        """
                        INSERT INTO users
                            (username, password_hash, recovery_code_hash,
                             recovery_issued_at, is_platform_admin)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            username,
                            generate_password_hash(password),
                            generate_password_hash(recovery_code),
                            utcnow_text(),
                            0,
                        ),
                    )
                    claim_orphaned_legacy_room(database, cursor.lastrowid)
                    database.commit()
                    session.clear()
                    session["user_id"] = cursor.lastrowid
                    session["session_version"] = 1
                    session["new_recovery_code"] = recovery_code
                    session.permanent = True
                    if pending_invite:
                        session["pending_invite_token"] = pending_invite
                    return redirect(url_for("account_security"))
                except sqlite3.IntegrityError:
                    database.rollback()
                    error = "这个账号名已经被使用，请换一个。"
                except sqlite3.OperationalError:
                    database.rollback()
                    error = "当前注册请求较多，请稍后重试。"
            flash(error, "error")

        return render_template("register.html")

    @app.route("/login", methods=("GET", "POST"))
    def login():
        if g.user is not None:
            return redirect(url_for("rooms"))

        if request.method == "POST":
            require_valid_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if rate_limit_exceeded(
                f"login:{client_ip()}:{username.casefold()}", 30, 300
            ):
                flash("登录尝试过于频繁，请稍后再试。", "error")
                return render_template("login.html"), 429
            user = get_db().execute(
                """
                SELECT id, username, password_hash, session_version,
                       recovery_code_hash
                FROM users
                WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()

            if user is None or not check_password_hash(user["password_hash"], password):
                flash("账号名或密码不正确。", "error")
            else:
                pending_invite = session.get("pending_invite_token")
                recovery_code = None
                if user["recovery_code_hash"] is None:
                    database = get_db()
                    database.execute("BEGIN IMMEDIATE")
                    locked_user = database.execute(
                        "SELECT recovery_code_hash FROM users WHERE id = ?",
                        (user["id"],),
                    ).fetchone()
                    if locked_user is not None and locked_user["recovery_code_hash"] is None:
                        recovery_code = generate_recovery_code()
                        database.execute(
                            """
                            UPDATE users
                            SET recovery_code_hash = ?, recovery_issued_at = ?
                            WHERE id = ?
                            """,
                            (
                                generate_password_hash(recovery_code),
                                utcnow_text(),
                                user["id"],
                            ),
                        )
                        audit_log(
                            database,
                            None,
                            user["id"],
                            "account.recovery_generated",
                            "user",
                            user["id"],
                        )
                    database.commit()
                session.clear()
                session["user_id"] = user["id"]
                session["session_version"] = user["session_version"]
                session.permanent = True
                if pending_invite:
                    session["pending_invite_token"] = pending_invite
                if recovery_code:
                    session["new_recovery_code"] = recovery_code
                    return redirect(url_for("account_security"))
                if pending_invite:
                    return redirect(url_for("accept_invite", token=pending_invite))
                return redirect(url_for("rooms"))

        return render_template("login.html")

    @app.post("/logout")
    def logout():
        require_valid_csrf()
        session.clear()
        return redirect(url_for("login"))

    @app.route("/forgot-password", methods=("GET", "POST"))
    def forgot_password():
        if g.user is not None:
            return redirect(url_for("account_security"))
        if request.method == "POST":
            require_valid_csrf()
            username = request.form.get("username", "").strip()
            recovery_value = request.form.get("recovery_code", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            password_error = validate_new_password(password, confirm_password)
            if rate_limit_exceeded(
                f"recover:{client_ip()}:{username.casefold()}", 12, 900
            ):
                flash("尝试次数过多，请稍后再试。", "error")
                return render_template("forgot_password.html"), 429
            if password_error:
                flash(password_error, "error")
                return render_template("forgot_password.html")

            database = get_db()
            database.execute("BEGIN IMMEDIATE")
            user = database.execute(
                """
                SELECT id, recovery_code_hash, session_version
                FROM users WHERE username = ? COLLATE NOCASE
                """,
                (username,),
            ).fetchone()
            valid = False
            if user is not None and user["recovery_code_hash"]:
                valid = check_password_hash(
                    user["recovery_code_hash"], recovery_value
                )
            if user is not None and not valid:
                reset_rows = database.execute(
                    """
                    SELECT id, token_hash
                    FROM password_reset_tokens
                    WHERE user_id = ? AND used_at IS NULL AND expires_at > ?
                    ORDER BY id DESC
                    """,
                    (user["id"], utcnow_text()),
                ).fetchall()
                for reset_row in reset_rows:
                    if check_password_hash(reset_row["token_hash"], recovery_value):
                        valid = True
                        break
            if user is None:
                check_password_hash(generate_password_hash("invalid"), recovery_value)

            if not valid:
                database.rollback()
                flash("账号信息或恢复凭证不正确。", "error")
                return render_template("forgot_password.html")

            new_recovery_code = generate_recovery_code()
            new_version = user["session_version"] + 1
            database.execute(
                """
                UPDATE users
                SET password_hash = ?, session_version = ?,
                    recovery_code_hash = ?, recovery_issued_at = ?
                WHERE id = ?
                """,
                (
                    generate_password_hash(password),
                    new_version,
                    generate_password_hash(new_recovery_code),
                    utcnow_text(),
                    user["id"],
                ),
            )
            # Any successful credential recovery invalidates every outstanding
            # operator-issued code, not only the code used for this request.
            database.execute(
                """
                UPDATE password_reset_tokens SET used_at = ?
                WHERE user_id = ? AND used_at IS NULL
                """,
                (utcnow_text(), user["id"]),
            )
            audit_log(
                database,
                None,
                user["id"],
                "account.password_reset",
                "user",
                user["id"],
            )
            database.commit()
            session.clear()
            session["user_id"] = user["id"]
            session["session_version"] = new_version
            session["new_recovery_code"] = new_recovery_code
            session.permanent = True
            flash("密码已重置，请保存新的账号恢复码。", "success")
            return redirect(url_for("account_security"))

        return render_template("forgot_password.html")

    @app.get("/user")
    @login_required
    def user_center():
        notification_count = get_db().execute(
            "SELECT COUNT(*) FROM user_notifications WHERE user_id = ?",
            (g.user["id"],),
        ).fetchone()[0]
        return render_template(
            "user_center.html", notification_count=notification_count
        )

    @app.get("/user/notifications")
    @login_required
    def user_notifications():
        notifications = get_db().execute(
            """
            SELECT n.*, u.username AS actor_name
            FROM user_notifications n
            LEFT JOIN users u ON u.id = n.actor_user_id
            WHERE n.user_id = ?
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT 100
            """,
            (g.user["id"],),
        ).fetchall()
        return render_template(
            "user_notifications.html", notifications=notifications
        )

    @app.get("/user/settings")
    @login_required
    def user_settings():
        return render_template("user_settings.html")

    @app.get("/account/security")
    @login_required
    def account_security():
        return render_template(
            "account_security.html",
            recovery_code=session.pop("new_recovery_code", None),
            admin_reset=session.pop("new_admin_reset", None),
            pending_invite_token=session.get("pending_invite_token"),
        )

    @app.post("/account/security/recovery-code")
    @login_required
    def regenerate_recovery_code():
        require_valid_csrf()
        password = request.form.get("password", "")
        database = get_db()
        database.execute("BEGIN IMMEDIATE")
        user = database.execute(
            "SELECT password_hash FROM users WHERE id = ?", (g.user["id"],)
        ).fetchone()
        if not check_password_hash(user["password_hash"], password):
            database.rollback()
            flash("当前密码不正确。", "error")
        else:
            recovery_code = generate_recovery_code()
            database.execute(
                """
                UPDATE users
                SET recovery_code_hash = ?, recovery_issued_at = ?
                WHERE id = ?
                """,
                (
                    generate_password_hash(recovery_code),
                    utcnow_text(),
                    g.user["id"],
                ),
            )
            database.execute(
                """
                UPDATE password_reset_tokens SET used_at = ?
                WHERE user_id = ? AND used_at IS NULL
                """,
                (utcnow_text(), g.user["id"]),
            )
            audit_log(
                database,
                None,
                g.user["id"],
                "account.recovery_generated",
                "user",
                g.user["id"],
            )
            database.commit()
            session["new_recovery_code"] = recovery_code
            flash("新的账号恢复码已生成，旧恢复码已失效。", "success")
        return redirect(url_for("account_security"))

    @app.post("/account/security/password")
    @login_required
    def change_password():
        require_valid_csrf()
        current_password = request.form.get("current_password", "")
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        error = validate_new_password(password, confirm_password)
        database = get_db()
        if error:
            flash(error, "error")
            return redirect(url_for("account_security"))
        database.execute("BEGIN IMMEDIATE")
        user = database.execute(
            "SELECT password_hash, session_version FROM users WHERE id = ?",
            (g.user["id"],),
        ).fetchone()
        if not check_password_hash(user["password_hash"], current_password):
            error = "当前密码不正确。"
        if error:
            database.rollback()
            flash(error, "error")
            return redirect(url_for("account_security"))

        recovery_code = generate_recovery_code()
        new_version = user["session_version"] + 1
        database.execute(
            """
            UPDATE users
            SET password_hash = ?, session_version = ?,
                recovery_code_hash = ?, recovery_issued_at = ?
            WHERE id = ?
            """,
            (
                generate_password_hash(password),
                new_version,
                generate_password_hash(recovery_code),
                utcnow_text(),
                g.user["id"],
            ),
        )
        database.execute(
            """
            UPDATE password_reset_tokens SET used_at = ?
            WHERE user_id = ? AND used_at IS NULL
            """,
            (utcnow_text(), g.user["id"]),
        )
        audit_log(
            database,
            None,
            g.user["id"],
            "account.password_changed",
            "user",
            g.user["id"],
        )
        database.commit()
        session["session_version"] = new_version
        session["new_recovery_code"] = recovery_code
        flash("密码已更新，其他设备的登录已失效。", "success")
        return redirect(url_for("account_security"))

    @app.post("/account/security/logout-others")
    @login_required
    def logout_other_sessions():
        require_valid_csrf()
        database = get_db()
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            "UPDATE users SET session_version = session_version + 1 WHERE id = ?",
            (g.user["id"],),
        )
        new_version = database.execute(
            "SELECT session_version FROM users WHERE id = ?", (g.user["id"],)
        ).fetchone()[0]
        audit_log(
            database,
            None,
            g.user["id"],
            "account.sessions_revoked",
            "user",
            g.user["id"],
        )
        database.commit()
        session["session_version"] = new_version
        flash("其他设备的登录已退出。", "success")
        return redirect(url_for("account_security"))

    @app.post("/admin/password-resets")
    @login_required
    def issue_admin_password_reset():
        require_valid_csrf()
        if not is_platform_admin(g.user):
            abort(403)
        username = request.form.get("username", "").strip()
        database = get_db()
        user = database.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if user is None:
            flash("未找到该账号。", "error")
            return redirect(url_for("account_security"))
        reset_code = generate_reset_code()
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        database.execute("BEGIN IMMEDIATE")
        database.execute(
            """
            INSERT INTO password_reset_tokens
                (user_id, token_hash, expires_at, created_by)
            VALUES (?, ?, ?, ?)
            """,
            (
                user["id"],
                generate_password_hash(reset_code),
                expires_at,
                g.user["id"],
            ),
        )
        audit_log(
            database,
            None,
            g.user["id"],
            "account.admin_reset_issued",
            "user",
            user["id"],
        )
        database.commit()
        session["new_admin_reset"] = {
            "username": user["username"],
            "code": reset_code,
            "expires_at": expires_at,
        }
        return redirect(url_for("account_security"))

    @app.get("/rooms")
    @login_required
    def rooms():
        room_rows = get_db().execute(
            """
            SELECT
                r.*,
                rm.role AS member_role,
                rm.can_score,
                CASE WHEN r.owner_id = rm.user_id THEN 1 ELSE 0 END AS is_owner,
                (SELECT COUNT(*) FROM candidates c
                 WHERE c.room_id = r.id AND c.archived = 0) AS candidate_count,
                (SELECT COUNT(*) FROM room_members members
                 WHERE members.room_id = r.id AND members.active = 1
                   AND members.can_score = 1 AND members.scores_included = 1) AS judge_count,
                (SELECT COUNT(*) FROM scores s
                 JOIN room_members members
                   ON members.room_id = s.room_id AND members.user_id = s.user_id
                 JOIN candidates c ON c.id = s.candidate_id AND c.room_id = s.room_id
                 WHERE s.room_id = r.id AND members.scores_included = 1
                   AND c.archived = 0)
                    AS submitted_count
            FROM room_members rm
            JOIN rooms r ON r.id = rm.room_id
            WHERE rm.user_id = ? AND rm.active = 1
            ORDER BY r.updated_at DESC, r.id DESC
            """,
            (g.user["id"],),
        ).fetchall()
        return render_template("rooms.html", rooms=room_rows)

    def render_help_center(slug: str):
        topic = HELP_TOPICS_BY_SLUG.get(slug)
        if topic is None:
            abort(404)
        return_path = safe_internal_return_path(
            request.args.get("return_to")
        ) or url_for("rooms")
        return render_template(
            "help.html",
            help_topics=HELP_TOPICS,
            topic=topic,
            return_path=return_path,
        )

    @app.get("/help")
    @login_required
    def help_center():
        return render_help_center(HELP_TOPICS[0]["slug"])

    @app.get("/help/<slug>")
    @login_required
    def help_topic(slug: str):
        return render_help_center(slug)

    @app.route("/rooms/create", methods=("GET", "POST"))
    @login_required
    def create_room():
        if request.method == "POST":
            require_valid_csrf()
            name = request.form.get("name", "").strip()
            join_password = request.form.get("join_password", "")
            error = None
            if not 2 <= len(name) <= 60:
                error = "房间名称需为 2–60 个字符。"
            elif not 4 <= len(join_password) <= 128:
                error = "加入密码需为 4–128 位。"

            try:
                score_min, score_max = parse_score_config(request.form)
            except ValueError as exc:
                score_min, score_max = 1, 5
                error = str(exc)

            if error is None:
                database = get_db()
                try:
                    database.execute("BEGIN IMMEDIATE")
                    code = generate_room_code(database)
                    result_visibility = request.form.get(
                        "result_visibility", "after_complete"
                    )
                    if result_visibility not in RESULT_VISIBILITY_LABELS:
                        result_visibility = "after_complete"
                    owner_can_score = int(
                        request.form.get("owner_can_score", "0") == "1"
                    )
                    cursor = database.execute(
                        """
                        INSERT INTO rooms
                            (name, code, join_password_hash, owner_id,
                             score_min, score_max, result_visibility)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            name,
                            code,
                            generate_password_hash(join_password),
                            g.user["id"],
                            score_min,
                            score_max,
                            result_visibility,
                        ),
                    )
                    room_id = cursor.lastrowid
                    database.execute(
                        """
                        INSERT INTO room_members
                            (room_id, user_id, role, can_score)
                        VALUES (?, ?, 'admin', ?)
                        """,
                        (room_id, g.user["id"], owner_can_score),
                    )
                    audit_log(
                        database,
                        room_id,
                        g.user["id"],
                        "room.created",
                        "room",
                        room_id,
                        {"name": name},
                    )
                    database.commit()
                    flash("房间已创建，请先添加候选人。", "success")
                    return redirect(url_for("room_settings", room_id=room_id))
                except sqlite3.OperationalError:
                    database.rollback()
                    error = "创建房间繁忙，请稍后重试。"

            flash(error, "error")

        return render_template("create_room.html")

    @app.route("/rooms/join", methods=("GET", "POST"))
    @login_required
    def join_room():
        if request.method == "POST":
            require_valid_csrf()
            code = normalize_room_code(request.form.get("code", ""))
            join_password = request.form.get("join_password", "")
            database = get_db()
            room = database.execute(
                "SELECT * FROM rooms WHERE code = ? COLLATE NOCASE", (code,)
            ).fetchone()
            existing = None
            if room is not None:
                existing = database.execute(
                    "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
                    (room["id"], g.user["id"]),
                ).fetchone()

            error = None
            if not ROOM_CODE_RE.fullmatch(code):
                error = "房间码格式不正确。"
            elif room is None:
                error = "没有找到这个房间。"
            elif existing is not None and existing["active"]:
                return redirect(url_for("room_dashboard", room_id=room["id"]))
            elif existing is not None:
                error = "你的房间成员资格已被管理员停用，请联系管理员恢复。"
            elif room["members_locked"]:
                error = "该房间已经锁定评委名单，暂时不能加入。"
            elif room["join_policy"] != "password":
                error = "该房间仅支持邀请链接加入。"
            elif not check_password_hash(room["join_password_hash"], join_password):
                error = "房间码或加入密码不正确。"

            if error is None:
                try:
                    database.execute("BEGIN IMMEDIATE")
                    locked_room = database.execute(
                        "SELECT * FROM rooms WHERE id = ?", (room["id"],)
                    ).fetchone()
                    locked_existing = database.execute(
                        "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
                        (room["id"], g.user["id"]),
                    ).fetchone()
                    if locked_existing is not None and locked_existing["active"]:
                        database.rollback()
                        return redirect(
                            url_for("room_dashboard", room_id=room["id"])
                        )
                    if locked_existing is not None:
                        error = "你的房间成员资格已被管理员停用，请联系房主恢复。"
                    elif locked_room is None:
                        error = "没有找到这个房间。"
                    elif locked_room["members_locked"]:
                        error = "该房间已经暂停新成员加入。"
                    elif locked_room["join_policy"] != "password":
                        error = "该房间仅支持邀请链接加入。"
                    elif not check_password_hash(
                        locked_room["join_password_hash"], join_password
                    ):
                        error = "房间码或加入密码不正确。"
                    if error is not None:
                        database.rollback()
                        flash(error, "error")
                        return render_template("join_room.html")
                    database.execute(
                        """
                        INSERT INTO room_members
                            (room_id, user_id, role, active, can_score,
                             scores_included, removed_at, removed_by, removal_reason)
                        VALUES (?, ?, 'judge', 1, 1, 1, NULL, NULL, NULL)
                        """,
                        (room["id"], g.user["id"]),
                    )
                    ensure_assignments_for_member(
                        database, room["id"], g.user["id"], include_existing=True
                    )
                    database.execute(
                        """
                        UPDATE rooms
                        SET updated_at = CURRENT_TIMESTAMP,
                            roster_revision = roster_revision + 1
                        WHERE id = ?
                        """,
                        (room["id"],),
                    )
                    audit_log(
                        database,
                        room["id"],
                        g.user["id"],
                        "member.joined",
                        "user",
                        g.user["id"],
                    )
                    database.commit()
                    flash("已加入房间。", "success")
                    return redirect(url_for("room_dashboard", room_id=room["id"]))
                except sqlite3.OperationalError:
                    database.rollback()
                    error = "加入请求繁忙，请稍后重试。"
                except sqlite3.IntegrityError:
                    database.rollback()
                    existing_now = database.execute(
                        "SELECT active FROM room_members WHERE room_id = ? AND user_id = ?",
                        (room["id"], g.user["id"]),
                    ).fetchone()
                    if existing_now is not None and existing_now["active"]:
                        return redirect(
                            url_for("room_dashboard", room_id=room["id"])
                        )
                    error = "加入请求未完成，请联系房主确认成员状态。"

            flash(error, "error")

        return render_template("join_room.html")

    @app.route("/invite/<token>", methods=("GET", "POST"))
    def accept_invite(token: str):
        if not valid_secret_token(token):
            abort(404)
        if g.user is None:
            session["pending_invite_token"] = token
            flash("请先登录或注册，再接受房间邀请。", "info")
            return redirect(url_for("login"))

        token_hash = hash_token(token)
        database = get_db()
        invitation = database.execute(
            """
            SELECT i.*, r.name AS room_name, r.code AS room_code
            FROM room_invitations i
            JOIN rooms r ON r.id = i.room_id
            WHERE i.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not invitation_is_valid(invitation):
            session.pop("pending_invite_token", None)
            return render_template("invite_accept.html", invitation=None), 410

        existing = database.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (invitation["room_id"], g.user["id"]),
        ).fetchone()
        if existing is not None and existing["active"]:
            session.pop("pending_invite_token", None)
            return redirect(
                url_for("room_dashboard", room_id=invitation["room_id"])
            )

        if request.method == "POST":
            require_valid_csrf()
            database.execute("BEGIN IMMEDIATE")
            locked_invitation = database.execute(
                "SELECT * FROM room_invitations WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if not invitation_is_valid(locked_invitation):
                database.rollback()
                flash("邀请已经失效或使用完毕。", "error")
                return redirect(url_for("accept_invite", token=token))
            existing = database.execute(
                "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
                (locked_invitation["room_id"], g.user["id"]),
            ).fetchone()
            if existing is not None:
                database.rollback()
                flash("你曾被移出该房间，请联系房主恢复成员资格。", "error")
                return redirect(url_for("rooms"))

            database.execute(
                """
                INSERT INTO room_members
                    (room_id, user_id, role, can_score, active, scores_included)
                VALUES (?, ?, ?, ?, 1, 1)
                """,
                (
                    locked_invitation["room_id"],
                    g.user["id"],
                    locked_invitation["role"],
                    locked_invitation["can_score"],
                ),
            )
            if locked_invitation["can_score"]:
                ensure_assignments_for_member(
                    database,
                    locked_invitation["room_id"],
                    g.user["id"],
                    include_existing=bool(locked_invitation["assign_existing"]),
                )
            database.execute(
                "UPDATE room_invitations SET used_count = used_count + 1 WHERE id = ?",
                (locked_invitation["id"],),
            )
            database.execute(
                """
                UPDATE rooms
                SET roster_revision = roster_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (locked_invitation["room_id"],),
            )
            audit_log(
                database,
                locked_invitation["room_id"],
                g.user["id"],
                "member.joined",
                "user",
                g.user["id"],
                {"via": "invite"},
            )
            database.commit()
            session.pop("pending_invite_token", None)
            flash("已加入房间。", "success")
            return redirect(
                url_for("room_dashboard", room_id=locked_invitation["room_id"])
            )

        return render_template("invite_accept.html", invitation=invitation)

    @app.get("/rooms/<int:room_id>")
    @login_required
    def room_dashboard(room_id: int):
        database = get_db()
        room = require_room_member(database, room_id, g.user["id"])
        candidates = database.execute(
            """
            SELECT
                c.id, c.candidate_no, c.name, c.group_name, c.notes, c.position,
                s.score AS user_score,
                ra.status AS assignment_status
            FROM candidates c
            LEFT JOIN scores s
              ON s.candidate_id = c.id
             AND s.room_id = c.room_id
             AND s.user_id = ?
            LEFT JOIN review_assignments ra
              ON ra.candidate_id = c.id
             AND ra.room_id = c.room_id
             AND ra.user_id = ?
            WHERE c.room_id = ? AND c.archived = 0
            ORDER BY c.position, c.id
            """,
            (g.user["id"], g.user["id"], room_id),
        ).fetchall()
        state = build_room_state(database, room_id, g.user["id"])
        score_span = room["score_max"] - room["score_min"] + 1
        quick_values = (
            list(range(room["score_min"], room["score_max"] + 1))
            if score_span <= 5
            else None
        )
        return render_template(
            "dashboard.html",
            room=room,
            candidates=candidates,
            state=state,
            quick_values=quick_values,
            can_score=(
                room["status"] == "scoring" and bool(room["member_can_score"])
            ),
        )

    @app.get("/rooms/<int:room_id>/settings")
    @login_required
    def room_settings(room_id: int):
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        score_count = database.execute(
            "SELECT COUNT(*) FROM scores WHERE room_id = ?", (room_id,)
        ).fetchone()[0]
        return render_template(
            "settings.html",
            room=room,
            score_count=score_count,
        )

    @app.get("/rooms/<int:room_id>/results")
    @login_required
    def room_results(room_id: int):
        database = get_db()
        room = require_room_member(database, room_id, g.user["id"])
        state = build_room_state(database, room_id, g.user["id"])
        return render_template("results.html", room=room, state=state)

    @app.get("/rooms/<int:room_id>/results/export.csv")
    @login_required
    def export_room_results(room_id: int):
        database = get_db()
        room = require_room_member(database, room_id, g.user["id"])
        state = build_room_state(database, room_id, g.user["id"])
        if not state["results_visible"]:
            abort(403)

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            (
                "排名",
                "候选人编号",
                "候选人姓名",
                "分组",
                "平均分",
                "有效评分数",
                "应评分数",
                "评分状态",
            )
        )
        for item in state["rankings"]:
            expected_count = item["expected_count"]
            score_count = item["score_count"]
            if expected_count == 0:
                completion = "暂无任务"
            elif score_count == expected_count:
                completion = "已完成"
            else:
                completion = f"缺 {expected_count - score_count} 份"
            writer.writerow(
                (
                    item["rank"] if item["rank"] is not None else "",
                    spreadsheet_safe_text(item["candidate_no"]),
                    spreadsheet_safe_text(item["name"]),
                    spreadsheet_safe_text(item["group_name"]),
                    f'{item["avg_score"]:.2f}'
                    if item["avg_score"] is not None
                    else "",
                    score_count,
                    expected_count,
                    completion,
                )
            )

        payload = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))
        response = send_file(
            payload,
            as_attachment=True,
            download_name=f"hengxuan-{room['code']}-results.csv",
            mimetype="text/csv; charset=utf-8",
            max_age=0,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/rooms/<int:room_id>/candidates")
    @login_required
    def room_candidates(room_id: int):
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        candidates = database.execute(
            """
            SELECT c.*, COUNT(s.user_id) AS score_count
            FROM candidates c
            LEFT JOIN scores s
              ON s.room_id = c.room_id AND s.candidate_id = c.id
            WHERE c.room_id = ?
            GROUP BY c.id
            ORDER BY c.archived, c.position, c.id
            """,
            (room_id,),
        ).fetchall()
        return render_template("candidates.html", room=room, candidates=candidates)

    @app.get("/rooms/<int:room_id>/members")
    @login_required
    def room_members_page(room_id: int):
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        members = build_member_details(database, room_id, g.user["id"])
        invitations = []
        invite_url = None
        invite_qr = None
        if room["is_owner"]:
            invitations = database.execute(
                """
                SELECT i.*, u.username AS creator_name
                FROM room_invitations i
                JOIN users u ON u.id = i.created_by
                WHERE i.room_id = ?
                ORDER BY i.created_at DESC, i.id DESC
                LIMIT 30
                """,
                (room_id,),
            ).fetchall()
            invite_payload = session.pop("new_invite", None)
            if invite_payload and invite_payload.get("room_id") == room_id:
                invite_url = url_for(
                    "accept_invite",
                    token=invite_payload["token"],
                    _external=True,
                )
                invite_qr = make_qr_data_url(invite_url)
        return render_template(
            "members.html",
            room=room,
            members=members,
            invitations=invitations,
            invite_url=invite_url,
            invite_qr=invite_qr,
        )

    @app.get("/rooms/<int:room_id>/audit")
    @login_required
    def room_audit(room_id: int):
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        events = database.execute(
            """
            SELECT a.*, u.username AS actor_name
            FROM audit_logs a
            LEFT JOIN users u ON u.id = a.actor_user_id
            WHERE a.room_id = ?
            ORDER BY a.id DESC
            LIMIT 100
            """,
            (room_id,),
        ).fetchall()
        return render_template("audit.html", room=room, events=events)

    @app.post("/rooms/<int:room_id>/settings/general")
    @login_required
    def update_room_general(room_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        name = request.form.get("name", "").strip()
        result_visibility = request.form.get(
            "result_visibility", room["result_visibility"]
        )
        join_policy = request.form.get("join_policy", room["join_policy"])
        if not 2 <= len(name) <= 60:
            flash("房间名称需为 2–60 个字符。", "error")
        elif result_visibility not in RESULT_VISIBILITY_LABELS:
            flash("结果可见策略无效。", "error")
        elif join_policy not in {"password", "invite_only"}:
            flash("加入方式无效。", "error")
        else:
            locked_room = begin_manager_mutation(database, room_id, g.user["id"])
            if not locked_room["is_owner"]:
                join_policy = locked_room["join_policy"]
            database.execute(
                """
                UPDATE rooms
                SET name = ?, result_visibility = ?, join_policy = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (name, result_visibility, join_policy, room_id),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "room.updated",
                "room",
                room_id,
                {
                    "name": name,
                    "result_visibility": result_visibility,
                    "join_policy": join_policy,
                },
            )
            database.commit()
            flash("房间名称已更新。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/settings/scheme")
    @login_required
    def update_room_scheme(room_id: int):
        require_valid_csrf()
        database = get_db()
        require_room_manager(database, room_id, g.user["id"])
        try:
            score_min, score_max = parse_score_config(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            begin_manager_mutation(database, room_id, g.user["id"])
            score_count = database.execute(
                "SELECT COUNT(*) FROM scores WHERE room_id = ?", (room_id,)
            ).fetchone()[0]
            if score_count:
                database.rollback()
                flash("已有评分后不能修改分制，以免破坏历史数据。", "error")
                return redirect(url_for("room_settings", room_id=room_id))
            database.execute(
                """
                UPDATE rooms
                SET score_min = ?, score_max = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (score_min, score_max, room_id),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "room.updated",
                "room",
                room_id,
                {"score_min": score_min, "score_max": score_max},
            )
            database.commit()
            flash("评分制度已更新。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/settings/password")
    @login_required
    def update_room_password(room_id: int):
        require_valid_csrf()
        database = get_db()
        require_room_owner(database, room_id, g.user["id"])
        join_password = request.form.get("join_password", "")
        if not 4 <= len(join_password) <= 128:
            flash("加入密码需为 4–128 位。", "error")
        else:
            begin_owner_mutation(database, room_id, g.user["id"])
            database.execute(
                """
                UPDATE rooms
                SET join_password_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (generate_password_hash(join_password), room_id),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "room.updated",
                "room",
                room_id,
                {"join_password_changed": True},
            )
            database.commit()
            flash("加入密码已更新。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/status")
    @login_required
    def update_room_status(room_id: int):
        require_valid_csrf()
        database = get_db()
        require_room_manager(database, room_id, g.user["id"])
        desired = request.form.get("status", "")
        if desired not in ROOM_STATUS_LABELS:
            flash("房间状态无效。", "error")
            return redirect(url_for("room_settings", room_id=room_id))

        begin_manager_mutation(database, room_id, g.user["id"])
        error = None
        if desired == "scoring":
            candidate_count = database.execute(
                "SELECT COUNT(*) FROM candidates WHERE room_id = ? AND archived = 0",
                (room_id,),
            ).fetchone()[0]
            if not candidate_count:
                error = "至少添加一位候选人后才能开始评分。"
        elif desired == "draft":
            score_count = database.execute(
                "SELECT COUNT(*) FROM scores WHERE room_id = ?", (room_id,)
            ).fetchone()[0]
            if score_count:
                error = "已有评分的房间不能退回准备状态，可以结束或重新开放评分。"
        if error:
            database.rollback()
            flash(error, "error")
            return redirect(url_for("room_settings", room_id=room_id))
        database.execute(
            "UPDATE rooms SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (desired, room_id),
        )
        audit_log(
            database,
            room_id,
            g.user["id"],
            "room.status_changed",
            "room",
            room_id,
            {"status": desired},
        )
        database.commit()
        flash(f"房间状态已切换为“{ROOM_STATUS_LABELS[desired]}”。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/members-lock")
    @login_required
    def toggle_members_lock(room_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_owner(database, room_id, g.user["id"])
        new_value = 0 if room["members_locked"] else 1
        locked_room = begin_owner_mutation(database, room_id, g.user["id"])
        new_value = 0 if locked_room["members_locked"] else 1
        database.execute(
            "UPDATE rooms SET members_locked = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (new_value, room_id),
        )
        audit_log(
            database,
            room_id,
            g.user["id"],
            "room.updated",
            "room",
            room_id,
            {"members_locked": bool(new_value)},
        )
        database.commit()
        flash("评委名单已锁定。" if new_value else "评委名单已开放。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/members/add")
    @login_required
    def add_room_member(room_id: int):
        require_valid_csrf()
        username = request.form.get("username", "").strip()
        role = request.form.get("role", "judge")
        can_score = int(request.form.get("can_score", "0") == "1")
        assign_existing = request.form.get("assign_existing", "0") == "1"
        if role not in {"admin", "judge"}:
            flash("成员身份无效。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        database = get_db()
        require_room_owner(database, room_id, g.user["id"])
        target_user = database.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
        if target_user is None:
            flash("无法添加该账号，请检查账号名。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))

        begin_owner_mutation(database, room_id, g.user["id"])
        existing = database.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, target_user["id"]),
        ).fetchone()
        if existing is not None and existing["active"]:
            database.rollback()
            flash("该账号已经在房间中。", "info")
            return redirect(url_for("room_members_page", room_id=room_id))
        database.execute(
            """
            INSERT INTO room_members
                (room_id, user_id, role, active, can_score, scores_included,
                 removed_at, removed_by, removal_reason)
            VALUES (?, ?, ?, 1, ?, 1, NULL, NULL, NULL)
            ON CONFLICT(room_id, user_id)
            DO UPDATE SET role = excluded.role, active = 1,
                          can_score = excluded.can_score,
                          removed_at = NULL, removed_by = NULL,
                          removal_reason = NULL
            """,
            (room_id, target_user["id"], role, can_score),
        )
        if can_score:
            ensure_assignments_for_member(
                database,
                room_id,
                target_user["id"],
                include_existing=assign_existing,
            )
        bump_roster_revision(database, room_id)
        audit_log(
            database,
            room_id,
            g.user["id"],
            "member.restored" if existing else "member.added",
            "user",
            target_user["id"],
            {"role": role, "can_score": bool(can_score)},
        )
        database.commit()
        flash("成员已添加。" if existing is None else "成员已恢复。", "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/members/<int:user_id>/toggle")
    @login_required
    def toggle_room_member(room_id: int, user_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_owner(database, room_id, g.user["id"])
        if user_id == room["owner_id"]:
            flash("房主不能被移出，请先转让房主。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        locked_room = begin_owner_mutation(database, room_id, g.user["id"])
        target = database.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        if target is None:
            database.rollback()
            abort(404)
        if target["active"]:
            reason = request.form.get("reason", "").strip()[:200] or None
            database.execute(
                """
                UPDATE room_members
                SET active = 0, removed_at = ?, removed_by = ?, removal_reason = ?
                WHERE room_id = ? AND user_id = ?
                """,
                (utcnow_text(), g.user["id"], reason, room_id, user_id),
            )
            database.execute(
                """
                UPDATE review_assignments
                SET status = 'waived', updated_at = ?
                WHERE room_id = ? AND user_id = ? AND status = 'required'
                """,
                (utcnow_text(), room_id, user_id),
            )
            create_user_notification(
                database,
                user_id,
                "room_member_removed",
                room_id,
                locked_room["name"],
                g.user["id"],
                reason,
            )
            action = "member.removed"
            message = "成员已移出，历史评分仍保留在结果中。"
        else:
            database.execute(
                """
                UPDATE room_members
                SET active = 1, removed_at = NULL, removed_by = NULL,
                    removal_reason = NULL
                WHERE room_id = ? AND user_id = ?
                """,
                (room_id, user_id),
            )
            if target["can_score"]:
                ensure_assignments_for_member(
                    database, room_id, user_id, include_existing=True
                )
            action = "member.restored"
            message = "成员已恢复。"
        bump_roster_revision(database, room_id)
        audit_log(
            database,
            room_id,
            g.user["id"],
            action,
            "user",
            user_id,
        )
        database.commit()
        flash(message, "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/members/<int:user_id>/role")
    @login_required
    def update_room_member_role(room_id: int, user_id: int):
        require_valid_csrf()
        role = request.form.get("role", "")
        can_score = int(request.form.get("can_score", "0") == "1")
        if role not in {"admin", "judge"}:
            flash("成员身份无效。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        database = get_db()
        room = require_room_owner(database, room_id, g.user["id"])
        if user_id == room["owner_id"]:
            role = "admin"
        begin_owner_mutation(database, room_id, g.user["id"])
        target = database.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        if target is None:
            database.rollback()
            abort(404)
        database.execute(
            """
            UPDATE room_members SET role = ?, can_score = ?
            WHERE room_id = ? AND user_id = ?
            """,
            (role, can_score, room_id, user_id),
        )
        if can_score and target["active"]:
            ensure_assignments_for_member(database, room_id, user_id, True)
        elif not can_score:
            database.execute(
                """
                UPDATE review_assignments
                SET status = 'waived', updated_at = ?
                WHERE room_id = ? AND user_id = ? AND status = 'required'
                """,
                (utcnow_text(), room_id, user_id),
            )
        bump_roster_revision(database, room_id)
        audit_log(
            database,
            room_id,
            g.user["id"],
            "member.updated",
            "user",
            user_id,
            {"role": role, "can_score": bool(can_score)},
        )
        database.commit()
        flash("成员权限已更新。", "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/members/<int:user_id>/score-inclusion")
    @login_required
    def update_member_score_inclusion(room_id: int, user_id: int):
        require_valid_csrf()
        include = request.form.get("include", "1") == "1"
        reason = request.form.get("reason", "").strip()
        database = get_db()
        require_room_owner(database, room_id, g.user["id"])
        begin_owner_mutation(database, room_id, g.user["id"])
        target = database.execute(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        ).fetchone()
        if target is None:
            database.rollback()
            abort(404)
        if target["active"] and not include:
            database.rollback()
            flash("不能直接作废有效成员的评分，请先将其移出房间。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        if not include and not reason:
            database.rollback()
            flash("作废历史评分时必须填写原因。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        database.execute(
            """
            UPDATE room_members SET scores_included = ?
            WHERE room_id = ? AND user_id = ?
            """,
            (int(include), room_id, user_id),
        )
        audit_log(
            database,
            room_id,
            g.user["id"],
            "member.scores_included" if include else "member.scores_excluded",
            "user",
            user_id,
            {"reason": reason} if reason else None,
        )
        database.commit()
        flash("历史评分计入状态已更新。", "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/owner-transfer")
    @login_required
    def transfer_room_owner(room_id: int):
        require_valid_csrf()
        try:
            target_user_id = int(request.form.get("user_id", ""))
        except ValueError:
            target_user_id = 0
        database = get_db()
        begin_owner_mutation(database, room_id, g.user["id"])
        target = database.execute(
            """
            SELECT * FROM room_members
            WHERE room_id = ? AND user_id = ? AND active = 1
            """,
            (room_id, target_user_id),
        ).fetchone()
        if target is None or target_user_id == g.user["id"]:
            database.rollback()
            flash("请选择另一位有效成员接任房主。", "error")
            return redirect(url_for("room_settings", room_id=room_id))
        database.execute(
            "UPDATE rooms SET owner_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (target_user_id, room_id),
        )
        database.execute(
            "UPDATE room_members SET role = 'admin' WHERE room_id = ? AND user_id IN (?, ?)",
            (room_id, g.user["id"], target_user_id),
        )
        audit_log(
            database,
            room_id,
            g.user["id"],
            "room.owner_transferred",
            "user",
            target_user_id,
        )
        database.commit()
        flash("房主已转让，你现在是协管员。", "success")
        return redirect(url_for("room_settings", room_id=room_id))

    @app.post("/rooms/<int:room_id>/invitations")
    @login_required
    def create_room_invitation(room_id: int):
        require_valid_csrf()
        role = request.form.get("role", "judge")
        can_score = int(request.form.get("can_score", "0") == "1")
        assign_existing = int(request.form.get("assign_existing", "0") == "1")
        expiry_days = INVITE_EXPIRY_DAYS.get(request.form.get("expiry", "7"))
        try:
            max_uses = int(request.form.get("max_uses", "1"))
        except ValueError:
            max_uses = 0
        if role not in {"admin", "judge"} or expiry_days is None or not 1 <= max_uses <= 100:
            flash("邀请设置无效。", "error")
            return redirect(url_for("room_members_page", room_id=room_id))
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        database = get_db()
        begin_owner_mutation(database, room_id, g.user["id"])
        cursor = database.execute(
            """
            INSERT INTO room_invitations
                (room_id, token_hash, role, can_score, assign_existing,
                 max_uses, expires_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id,
                hash_token(token),
                role,
                can_score,
                assign_existing,
                max_uses,
                expires_at,
                g.user["id"],
            ),
        )
        audit_log(
            database,
            room_id,
            g.user["id"],
            "invite.created",
            "invitation",
            cursor.lastrowid,
            {"role": role, "can_score": bool(can_score), "max_uses": max_uses},
        )
        database.commit()
        session["new_invite"] = {"room_id": room_id, "token": token}
        flash("邀请已生成。", "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/invitations/<int:invitation_id>/revoke")
    @login_required
    def revoke_room_invitation(room_id: int, invitation_id: int):
        require_valid_csrf()
        database = get_db()
        begin_owner_mutation(database, room_id, g.user["id"])
        cursor = database.execute(
            """
            UPDATE room_invitations SET revoked_at = ?
            WHERE id = ? AND room_id = ? AND revoked_at IS NULL
            """,
            (utcnow_text(), invitation_id, room_id),
        )
        if cursor.rowcount != 1:
            database.rollback()
            abort(404)
        audit_log(
            database,
            room_id,
            g.user["id"],
            "invite.revoked",
            "invitation",
            invitation_id,
        )
        database.commit()
        flash("邀请已撤销。", "success")
        return redirect(url_for("room_members_page", room_id=room_id))

    @app.post("/rooms/<int:room_id>/candidates")
    @login_required
    def add_candidate(room_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_manager(database, room_id, g.user["id"])
        values, error = candidate_form_values(request.form)
        if room["status"] == "closed" and request.form.get("reopen") != "1":
            error = "房间已结束；如需添加，请确认添加并重新开放评分。"
        if error:
            flash(error, "error")
        else:
            locked_room = begin_manager_mutation(database, room_id, g.user["id"])
            if locked_room["status"] == "closed" and request.form.get("reopen") != "1":
                database.rollback()
                flash("房间已结束；如需添加，请确认添加并重新开放评分。", "error")
                return redirect(url_for("room_candidates", room_id=room_id))
            if values["candidate_no"]:
                duplicate = database.execute(
                    """
                    SELECT 1 FROM candidates
                    WHERE room_id = ? AND candidate_no = ? COLLATE NOCASE
                    """,
                    (room_id, values["candidate_no"]),
                ).fetchone()
                if duplicate:
                    database.rollback()
                    flash("该候选人编号已存在。", "error")
                    return redirect(url_for("room_candidates", room_id=room_id))
            next_position = database.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM candidates WHERE room_id = ?",
                (room_id,),
            ).fetchone()[0]
            position = values["position"] or next_position
            cursor = database.execute(
                """
                INSERT INTO candidates
                    (room_id, candidate_no, name, group_name, notes, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    values["candidate_no"],
                    values["name"],
                    values["group_name"],
                    values["notes"],
                    position,
                ),
            )
            candidate_id = cursor.lastrowid
            ensure_assignments_for_candidate(database, room_id, candidate_id)
            database.execute(
                """
                UPDATE rooms
                SET status = CASE
                        WHEN status = 'closed' AND ? = 1 THEN 'scoring'
                        ELSE status
                    END,
                    roster_revision = roster_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(request.form.get("reopen") == "1"), room_id),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "candidate.created",
                "candidate",
                candidate_id,
                {"name": values["name"]},
            )
            database.commit()
            flash("候选人已添加。", "success")
        return redirect(url_for("room_candidates", room_id=room_id))

    @app.post("/rooms/<int:room_id>/candidates/<int:candidate_id>/edit")
    @login_required
    def edit_candidate(room_id: int, candidate_id: int):
        require_valid_csrf()
        database = get_db()
        require_room_manager(database, room_id, g.user["id"])
        candidate = database.execute(
            "SELECT * FROM candidates WHERE id = ? AND room_id = ?",
            (candidate_id, room_id),
        ).fetchone()
        if candidate is None:
            abort(404)
        values, error = candidate_form_values(request.form, position_required=True)
        if error:
            flash(error, "error")
        else:
            begin_manager_mutation(database, room_id, g.user["id"])
            if values["candidate_no"]:
                duplicate = database.execute(
                    """
                    SELECT 1 FROM candidates
                    WHERE room_id = ? AND id <> ?
                      AND candidate_no = ? COLLATE NOCASE
                    """,
                    (room_id, candidate_id, values["candidate_no"]),
                ).fetchone()
                if duplicate:
                    database.rollback()
                    flash("该候选人编号已存在。", "error")
                    return redirect(url_for("room_candidates", room_id=room_id))
            database.execute(
                """
                UPDATE candidates
                SET candidate_no = ?, name = ?, group_name = ?, notes = ?,
                    position = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND room_id = ?
                """,
                (
                    values["candidate_no"],
                    values["name"],
                    values["group_name"],
                    values["notes"],
                    values["position"],
                    candidate_id,
                    room_id,
                ),
            )
            database.execute(
                """
                UPDATE rooms
                SET roster_revision = roster_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (room_id,),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "candidate.updated",
                "candidate",
                candidate_id,
                {"name": values["name"]},
            )
            database.commit()
            flash("候选人信息已更新。", "success")
        return redirect(url_for("room_candidates", room_id=room_id))

    @app.post("/rooms/<int:room_id>/candidates/<int:candidate_id>/archive")
    @login_required
    def archive_candidate(room_id: int, candidate_id: int):
        require_valid_csrf()
        database = get_db()
        require_room_manager(database, room_id, g.user["id"])
        candidate = database.execute(
            "SELECT * FROM candidates WHERE id = ? AND room_id = ?",
            (candidate_id, room_id),
        ).fetchone()
        if candidate is None:
            abort(404)
        new_value = 0 if candidate["archived"] else 1
        begin_manager_mutation(database, room_id, g.user["id"])
        locked_candidate = database.execute(
            "SELECT archived FROM candidates WHERE id = ? AND room_id = ?",
            (candidate_id, room_id),
        ).fetchone()
        if locked_candidate is None:
            database.rollback()
            abort(404)
        new_value = 0 if locked_candidate["archived"] else 1
        database.execute(
            "UPDATE candidates SET archived = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND room_id = ?",
            (new_value, candidate_id, room_id),
        )
        if not new_value:
            ensure_assignments_for_candidate(database, room_id, candidate_id)
        database.execute(
            """
            UPDATE rooms
            SET roster_revision = roster_revision + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (room_id,),
        )
        action = "candidate.archived" if new_value else "candidate.restored"
        audit_log(
            database,
            room_id,
            g.user["id"],
            action,
            "candidate",
            candidate_id,
        )
        database.commit()
        flash("候选人已恢复。" if not new_value else "候选人已归档。", "success")
        return redirect(url_for("room_candidates", room_id=room_id))

    @app.get("/api/rooms/<int:room_id>/rankings")
    @login_required
    def rankings_api(room_id: int):
        database = get_db()
        require_room_member(database, room_id, g.user["id"])
        return jsonify(build_room_state(database, room_id, g.user["id"]))

    @app.post("/api/rooms/<int:room_id>/scores")
    @login_required
    def save_score(room_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_member(database, room_id, g.user["id"])
        if room["status"] != "scoring":
            return jsonify({"error": "当前房间不在评分状态。"}), 409
        if not room["member_can_score"]:
            return jsonify({"error": "你没有参与该房间评分。"}), 403

        payload = request.get_json(silent=True) or {}
        candidate_id = payload.get("candidate_id")
        score = payload.get("score")
        if type(candidate_id) is not int or type(score) is not int:
            return jsonify({"error": "评分必须是整数。"}), 400
        if not room["score_min"] <= score <= room["score_max"]:
            return jsonify(
                {"error": f"评分必须在 {room['score_min']}–{room['score_max']} 之间。"}
            ), 400

        try:
            database.execute("BEGIN IMMEDIATE")
            locked_room = database.execute(
                """
                SELECT r.status, r.score_min, r.score_max,
                       rm.active, rm.can_score, rm.scores_included
                FROM rooms r
                JOIN room_members rm ON rm.room_id = r.id
                WHERE r.id = ? AND rm.user_id = ?
                """,
                (room_id, g.user["id"]),
            ).fetchone()
            if (
                locked_room is None
                or not locked_room["active"]
                or not locked_room["can_score"]
                or not locked_room["scores_included"]
            ):
                database.rollback()
                return jsonify({"error": "你已无权在该房间评分。"}), 403
            if locked_room["status"] != "scoring":
                database.rollback()
                return jsonify({"error": "当前房间不在评分状态。"}), 409
            if not locked_room["score_min"] <= score <= locked_room["score_max"]:
                database.rollback()
                return jsonify(
                    {
                        "error": (
                            f"评分必须在 {locked_room['score_min']}–"
                            f"{locked_room['score_max']} 之间。"
                        )
                    }
                ), 400
            candidate = database.execute(
                """
                SELECT id FROM candidates
                WHERE id = ? AND room_id = ? AND archived = 0
                """,
                (candidate_id, room_id),
            ).fetchone()
            if candidate is None:
                database.rollback()
                return jsonify({"error": "未找到这位候选人。"}), 404
            assignment = database.execute(
                """
                SELECT status FROM review_assignments
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (room_id, g.user["id"], candidate_id),
            ).fetchone()
            if assignment is None or assignment["status"] == "waived":
                database.rollback()
                return jsonify({"error": "这位候选人未分配给你评分。"}), 403
            existing_score = database.execute(
                """
                SELECT score FROM scores
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (room_id, g.user["id"], candidate_id),
            ).fetchone()
            database.execute(
                """
                INSERT INTO scores (room_id, user_id, candidate_id, score, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(room_id, user_id, candidate_id)
                DO UPDATE SET score = excluded.score, updated_at = CURRENT_TIMESTAMP
                """,
                (room_id, g.user["id"], candidate_id, score),
            )
            database.execute(
                """
                UPDATE review_assignments
                SET status = 'completed', updated_at = ?
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (utcnow_text(), room_id, g.user["id"], candidate_id),
            )
            audit_log(
                database,
                room_id,
                g.user["id"],
                "score.saved",
                "candidate",
                candidate_id,
                {
                    "previous": existing_score["score"] if existing_score else None,
                    "score": score,
                },
            )
            database.commit()
        except sqlite3.OperationalError:
            database.rollback()
            return jsonify({"error": "评分保存繁忙，请重试。"}), 503

        state = build_room_state(database, room_id, g.user["id"])
        state["saved"] = {"candidate_id": candidate_id, "score": score}
        return jsonify(state)

    @app.delete("/api/rooms/<int:room_id>/scores/<int:candidate_id>")
    @login_required
    def remove_score(room_id: int, candidate_id: int):
        require_valid_csrf()
        database = get_db()
        room = require_room_member(database, room_id, g.user["id"])
        if room["status"] != "scoring":
            return jsonify({"error": "当前房间不在评分状态。"}), 409
        if not room["member_can_score"]:
            return jsonify({"error": "你没有参与该房间评分。"}), 403
        try:
            database.execute("BEGIN IMMEDIATE")
            locked_member = database.execute(
                """
                SELECT r.status, rm.active, rm.can_score, rm.scores_included
                FROM rooms r
                JOIN room_members rm ON rm.room_id = r.id
                WHERE r.id = ? AND rm.user_id = ?
                """,
                (room_id, g.user["id"]),
            ).fetchone()
            if (
                locked_member is None
                or not locked_member["active"]
                or not locked_member["can_score"]
                or not locked_member["scores_included"]
            ):
                database.rollback()
                return jsonify({"error": "你已无权在该房间评分。"}), 403
            if locked_member["status"] != "scoring":
                database.rollback()
                return jsonify({"error": "当前房间不在评分状态。"}), 409
            candidate = database.execute(
                "SELECT id FROM candidates WHERE id = ? AND room_id = ?",
                (candidate_id, room_id),
            ).fetchone()
            if candidate is None:
                database.rollback()
                return jsonify({"error": "未找到这位候选人。"}), 404
            deleted = database.execute(
                """
                DELETE FROM scores
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (room_id, g.user["id"], candidate_id),
            )
            assignment = database.execute(
                """
                SELECT status FROM review_assignments
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (room_id, g.user["id"], candidate_id),
            ).fetchone()
            if assignment is None or assignment["status"] == "waived":
                database.rollback()
                return jsonify({"error": "这位候选人未分配给你评分。"}), 403
            database.execute(
                """
                UPDATE review_assignments
                SET status = 'required', updated_at = ?
                WHERE room_id = ? AND user_id = ? AND candidate_id = ?
                """,
                (utcnow_text(), room_id, g.user["id"], candidate_id),
            )
            if deleted.rowcount:
                audit_log(
                    database,
                    room_id,
                    g.user["id"],
                    "score.removed",
                    "candidate",
                    candidate_id,
                )
            database.commit()
        except sqlite3.OperationalError:
            database.rollback()
            return jsonify({"error": "撤销操作繁忙，请重试。"}), 503

        state = build_room_state(database, room_id, g.user["id"])
        state["removed"] = {"candidate_id": candidate_id}
        return jsonify(state)

    @app.get("/healthz")
    def healthz():
        database = get_db()
        database.execute("SELECT 1").fetchone()
        version = database.execute("PRAGMA user_version").fetchone()[0]
        return jsonify({"status": "ok", "schema_version": version})

    return app


def validate_account(username: str, password: str, confirm_password: str) -> str | None:
    if not USERNAME_RE.fullmatch(username):
        return "账号名需为 2–20 个中文、字母、数字、下划线或短横线。"
    return validate_new_password(password, confirm_password)


def validate_new_password(password: str, confirm_password: str) -> str | None:
    if len(password) < 6:
        return "密码至少需要 6 位。"
    if len(password) > 128:
        return "密码不能超过 128 位。"
    if password != confirm_password:
        return "两次输入的密码不一致。"
    return None


def utcnow_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def spreadsheet_safe_text(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


def generate_recovery_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(5)]
    return "-".join(groups)


def generate_reset_code() -> str:
    return f"RESET-{generate_recovery_code()}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_secret_token(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token))


def invitation_is_valid(invitation: sqlite3.Row | None) -> bool:
    return bool(
        invitation is not None
        and invitation["revoked_at"] is None
        and invitation["used_count"] < invitation["max_uses"]
        and invitation["expires_at"] > utcnow_text()
    )


def make_qr_data_url(value: str) -> str:
    image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def client_ip() -> str:
    return request.remote_addr or "unknown"


def rate_limit_exceeded(key: str, limit: int, window_seconds: int) -> bool:
    if current_app.config.get("TESTING"):
        return False
    now = time.monotonic()
    cutoff = now - window_seconds
    with _RATE_LIMIT_LOCK:
        events = [value for value in _RATE_LIMIT_EVENTS.get(key, []) if value > cutoff]
        exceeded = len(events) >= limit
        if not exceeded:
            events.append(now)
        _RATE_LIMIT_EVENTS[key] = events
        return exceeded


def is_platform_admin(user: sqlite3.Row | None = None) -> bool:
    selected = user if user is not None else getattr(g, "user", None)
    if selected is None:
        return False
    return bool(selected["is_platform_admin"]) or (
        selected["id"] in current_app.config.get("PLATFORM_ADMIN_USER_IDS", set())
    )


def audit_log(
    database: sqlite3.Connection,
    room_id: int | None,
    actor_user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    database.execute(
        """
        INSERT INTO audit_logs
            (room_id, actor_user_id, action, target_type, target_id, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            room_id,
            actor_user_id,
            action,
            target_type,
            target_id,
            json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def create_user_notification(
    database: sqlite3.Connection,
    user_id: int,
    kind: str,
    room_id: int | None,
    room_name: str,
    actor_user_id: int | None,
    reason: str | None = None,
) -> None:
    database.execute(
        """
        INSERT INTO user_notifications
            (user_id, kind, room_id, room_name, actor_user_id, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, kind, room_id, room_name, actor_user_id, reason),
    )


def bump_roster_revision(database: sqlite3.Connection, room_id: int) -> None:
    database.execute(
        """
        UPDATE rooms
        SET roster_revision = roster_revision + 1,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (room_id,),
    )


def ensure_assignments_for_member(
    database: sqlite3.Connection,
    room_id: int,
    user_id: int,
    include_existing: bool,
) -> None:
    if not include_existing:
        return
    candidate_rows = database.execute(
        "SELECT id FROM candidates WHERE room_id = ? AND archived = 0",
        (room_id,),
    ).fetchall()
    for candidate in candidate_rows:
        has_score = database.execute(
            """
            SELECT 1 FROM scores
            WHERE room_id = ? AND user_id = ? AND candidate_id = ?
            """,
            (room_id, user_id, candidate["id"]),
        ).fetchone()
        status = "completed" if has_score else "required"
        database.execute(
            """
            INSERT INTO review_assignments
                (room_id, user_id, candidate_id, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(room_id, user_id, candidate_id)
            DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (room_id, user_id, candidate["id"], status, utcnow_text()),
        )


def ensure_assignments_for_candidate(
    database: sqlite3.Connection, room_id: int, candidate_id: int
) -> None:
    member_rows = database.execute(
        """
        SELECT user_id FROM room_members
        WHERE room_id = ? AND active = 1 AND can_score = 1
        """,
        (room_id,),
    ).fetchall()
    for member in member_rows:
        has_score = database.execute(
            """
            SELECT 1 FROM scores
            WHERE room_id = ? AND user_id = ? AND candidate_id = ?
            """,
            (room_id, member["user_id"], candidate_id),
        ).fetchone()
        status = "completed" if has_score else "required"
        database.execute(
            """
            INSERT INTO review_assignments
                (room_id, user_id, candidate_id, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(room_id, user_id, candidate_id)
            DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at
            """,
            (room_id, member["user_id"], candidate_id, status, utcnow_text()),
        )


def ensure_all_assignments(database: sqlite3.Connection, room_id: int) -> None:
    candidate_rows = database.execute(
        "SELECT id FROM candidates WHERE room_id = ? AND archived = 0",
        (room_id,),
    ).fetchall()
    for candidate in candidate_rows:
        ensure_assignments_for_candidate(database, room_id, candidate["id"])


def begin_owner_mutation(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    database.execute("BEGIN IMMEDIATE")
    room = database.execute(
        "SELECT * FROM rooms WHERE id = ?", (room_id,)
    ).fetchone()
    if room is None:
        database.rollback()
        abort(404)
    if room["owner_id"] != user_id:
        database.rollback()
        abort(403)
    return room


def begin_manager_mutation(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    database.execute("BEGIN IMMEDIATE")
    room = database.execute(
        """
        SELECT r.*, rm.role AS member_role,
               CASE WHEN rm.can_score = 1 AND rm.scores_included = 1
                    THEN 1 ELSE 0 END AS member_can_score,
               rm.scores_included AS member_scores_included,
               CASE WHEN r.owner_id = rm.user_id THEN 1 ELSE 0 END AS is_owner
        FROM rooms r
        JOIN room_members rm ON rm.room_id = r.id
        WHERE r.id = ? AND rm.user_id = ? AND rm.active = 1
        """,
        (room_id, user_id),
    ).fetchone()
    if room is None:
        database.rollback()
        abort(404)
    if not room["is_owner"] and room["member_role"] != "admin":
        database.rollback()
        abort(403)
    return room


def parse_score_config(form) -> tuple[int, int]:
    scheme = form.get("scheme", "5")
    if scheme in SCHEME_PRESETS:
        return SCHEME_PRESETS[scheme]
    if scheme != "custom":
        raise ValueError("请选择有效的评分制度。")
    try:
        score_min = int(form.get("score_min", ""))
        score_max = int(form.get("score_max", ""))
    except (TypeError, ValueError):
        raise ValueError("自定义最低分和最高分必须是整数。") from None
    if not 0 <= score_min < score_max <= 1000:
        raise ValueError("自定义分制需满足 0 ≤ 最低分 < 最高分 ≤ 1000。")
    return score_min, score_max


def candidate_form_values(form, position_required: bool = False):
    name = form.get("name", "").strip()
    candidate_no = form.get("candidate_no", "").strip() or None
    group_name = form.get("group_name", "").strip() or None
    notes = form.get("notes", "").strip() or None
    raw_position = form.get("position", "").strip()
    position = None
    if raw_position:
        try:
            position = int(raw_position)
        except ValueError:
            return {}, "排序必须是整数。"
    if not 1 <= len(name) <= 50:
        return {}, "候选人姓名需为 1–50 个字符。"
    if candidate_no and len(candidate_no) > 30:
        return {}, "候选人编号不能超过 30 个字符。"
    if group_name and len(group_name) > 50:
        return {}, "分组或部门不能超过 50 个字符。"
    if notes and len(notes) > 300:
        return {}, "备注不能超过 300 个字符。"
    if position_required and position is None:
        return {}, "请填写排序。"
    if position is not None and not 1 <= position <= 9999:
        return {}, "排序需在 1–9999 之间。"
    return {
        "name": name,
        "candidate_no": candidate_no,
        "group_name": group_name,
        "notes": notes,
        "position": position,
    }, None


def normalize_room_code(code: str) -> str:
    return "".join(character for character in code.upper().strip() if character.isalnum())


def generate_room_code(database: sqlite3.Connection) -> str:
    for _ in range(30):
        code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(7))
        exists = database.execute("SELECT 1 FROM rooms WHERE code = ?", (code,)).fetchone()
        if exists is None:
            return code
    raise sqlite3.OperationalError("unable to allocate room code")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        database = sqlite3.connect(
            current_app.config["DATABASE"],
            timeout=10,
            isolation_level=None,
        )
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys = ON")
        database.execute("PRAGMA busy_timeout = 10000")
        database.execute("PRAGMA journal_mode = WAL")
        g.db = database
    return g.db


def init_db(app: Flask) -> None:
    database = sqlite3.connect(app.config["DATABASE"], timeout=20)
    database.row_factory = sqlite3.Row
    try:
        database.execute("PRAGMA journal_mode = WAL")
        database.execute("PRAGMA busy_timeout = 20000")
        database.execute("PRAGMA foreign_keys = ON")
        version = database.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，"
                "请先升级程序。"
            )
        tables = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        if "candidates" in tables:
            columns = {
                row["name"]
                for row in database.execute("PRAGMA table_info(candidates)").fetchall()
            }
        else:
            columns = set()

        if "candidates" in tables and "room_id" not in columns:
            migrate_legacy_database(database)
            version = 2

        tables = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        }
        if "users" not in tables:
            database.execute("BEGIN IMMEDIATE")
            create_current_schema(database)
            database.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, name) "
                "VALUES (?, ?)",
                (SCHEMA_VERSION, "fresh_v4_schema"),
            )
            database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            database.commit()
        elif version < SCHEMA_VERSION:
            if version < 3:
                migrate_v2_to_v3(database)
                version = 3
            if version < 4:
                migrate_v3_to_v4(database)
        else:
            create_current_schema(database)
            validate_v4_schema(database)
            database.commit()

        database.execute("PRAGMA optimize")
        database.commit()
        violations = database.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(f"foreign key check failed: {violations}")
    finally:
        database.close()


def create_current_schema(database: sqlite3.Connection) -> None:
    for statement in (
        USERS_SQL,
        ROOMS_SQL,
        ROOM_MEMBERS_SQL,
        CANDIDATES_SQL,
        SCORES_SQL,
        REVIEW_ASSIGNMENTS_SQL,
        ROOM_INVITATIONS_SQL,
        PASSWORD_RESET_TOKENS_SQL,
        AUDIT_LOGS_SQL,
        USER_NOTIFICATIONS_SQL,
        SCHEMA_MIGRATIONS_SQL,
    ):
        database.execute(statement)
    for statement in INDEX_STATEMENTS:
        database.execute(statement)


def table_columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in database.execute(f"PRAGMA table_info({table})").fetchall()
    }


def ensure_column(
    database: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    if column not in table_columns(database, table):
        database.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_v2_to_v3(database: sqlite3.Connection) -> None:
    """Upgrade the room-based V2 schema without rebuilding score history."""
    database.execute("PRAGMA foreign_keys = ON")
    try:
        database.execute("BEGIN IMMEDIATE")
        ensure_column(
            database,
            "users",
            "session_version",
            "INTEGER NOT NULL DEFAULT 1 CHECK (session_version >= 1)",
        )
        ensure_column(database, "users", "recovery_code_hash", "TEXT")
        ensure_column(database, "users", "recovery_issued_at", "TEXT")
        ensure_column(
            database,
            "users",
            "is_platform_admin",
            "INTEGER NOT NULL DEFAULT 0 CHECK (is_platform_admin IN (0, 1))",
        )
        ensure_column(
            database,
            "rooms",
            "join_policy",
            "TEXT NOT NULL DEFAULT 'password' "
            "CHECK (join_policy IN ('password', 'invite_only'))",
        )
        # V2 always exposed live results; retain that behavior for upgraded rooms.
        ensure_column(
            database,
            "rooms",
            "result_visibility",
            "TEXT NOT NULL DEFAULT 'live' "
            "CHECK (result_visibility IN ('live', 'after_complete', 'after_closed'))",
        )
        ensure_column(
            database,
            "rooms",
            "roster_revision",
            "INTEGER NOT NULL DEFAULT 1 CHECK (roster_revision >= 1)",
        )
        ensure_column(
            database,
            "room_members",
            "can_score",
            "INTEGER NOT NULL DEFAULT 1 CHECK (can_score IN (0, 1))",
        )
        added_scores_included = "scores_included" not in table_columns(
            database, "room_members"
        )
        ensure_column(
            database,
            "room_members",
            "scores_included",
            "INTEGER NOT NULL DEFAULT 1 CHECK (scores_included IN (0, 1))",
        )
        ensure_column(database, "room_members", "removed_at", "TEXT")
        ensure_column(database, "room_members", "removed_by", "INTEGER")
        ensure_column(database, "room_members", "removal_reason", "TEXT")

        for statement in (
            REVIEW_ASSIGNMENTS_SQL,
            ROOM_INVITATIONS_SQL,
            PASSWORD_RESET_TOKENS_SQL,
            AUDIT_LOGS_SQL,
            SCHEMA_MIGRATIONS_SQL,
        ):
            database.execute(statement)

        if added_scores_included:
            # V2 excluded inactive members from rankings. Preserve that outcome on
            # upgrade while future removals keep scores unless the owner excludes them.
            database.execute(
                "UPDATE room_members SET scores_included = CASE active WHEN 1 THEN 1 ELSE 0 END"
            )

        orphaned_rooms = database.execute(
            "SELECT id FROM rooms WHERE owner_id IS NULL ORDER BY id"
        ).fetchall()
        for orphaned_room in orphaned_rooms:
            replacement = database.execute(
                """
                SELECT user_id FROM room_members
                WHERE room_id = ?
                ORDER BY active DESC, CASE role WHEN 'admin' THEN 0 ELSE 1 END,
                         joined_at, user_id
                LIMIT 1
                """,
                (orphaned_room["id"],),
            ).fetchone()
            if replacement is not None:
                database.execute(
                    "UPDATE rooms SET owner_id = ? WHERE id = ?",
                    (replacement["user_id"], orphaned_room["id"]),
                )

        database.execute(
            """
            UPDATE room_members
            SET role = 'admin', active = 1
            WHERE (room_id, user_id) IN (
                SELECT id, owner_id FROM rooms WHERE owner_id IS NOT NULL
            )
            """
        )

        # Preserve every historical score as a completed assignment first.
        database.execute(
            """
            INSERT OR IGNORE INTO review_assignments
                (room_id, user_id, candidate_id, status)
            SELECT room_id, user_id, candidate_id, 'completed' FROM scores
            """
        )
        # Then create the outstanding matrix for active scoring members/candidates.
        database.execute(
            """
            INSERT OR IGNORE INTO review_assignments
                (room_id, user_id, candidate_id, status)
            SELECT rm.room_id, rm.user_id, c.id,
                   CASE WHEN s.candidate_id IS NULL THEN 'required' ELSE 'completed' END
            FROM room_members rm
            JOIN candidates c ON c.room_id = rm.room_id AND c.archived = 0
            LEFT JOIN scores s
              ON s.room_id = rm.room_id AND s.user_id = rm.user_id
             AND s.candidate_id = c.id
            WHERE rm.active = 1 AND rm.can_score = 1
            """
        )
        for statement in V3_INDEX_STATEMENTS:
            database.execute(statement)
        database.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (3, ?)",
            ("owner_invites_assignments_recovery",),
        )
        validate_v3_schema(database)
        violations = database.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"foreign key check failed during V3 migration: {violations}"
            )
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(
                f"integrity check failed during V3 migration: {integrity}"
            )
        database.execute("PRAGMA user_version = 3")
        database.commit()
    except Exception:
        database.rollback()
        raise


def validate_v3_schema(database: sqlite3.Connection) -> None:
    required = {
        "users": {"session_version", "recovery_code_hash", "is_platform_admin"},
        "rooms": {"join_policy", "result_visibility", "roster_revision"},
        "room_members": {"can_score", "scores_included", "removed_at"},
        "review_assignments": {"room_id", "user_id", "candidate_id", "status"},
        "room_invitations": {"token_hash", "max_uses", "used_count"},
        "password_reset_tokens": {"token_hash", "used_at"},
        "audit_logs": {"action", "metadata_json"},
    }
    missing = {
        table: sorted(columns - table_columns(database, table))
        for table, columns in required.items()
        if columns - table_columns(database, table)
    }
    if missing:
        raise sqlite3.DatabaseError(f"V3 schema validation failed: {missing}")


def migrate_v3_to_v4(database: sqlite3.Connection) -> None:
    """Add persistent user notifications without changing room business data."""
    database.execute("PRAGMA foreign_keys = ON")
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(USER_NOTIFICATIONS_SQL)
        database.execute(INDEX_STATEMENTS[-1])
        database.execute(
            """
            INSERT INTO user_notifications
                (user_id, kind, room_id, room_name, actor_user_id, reason, created_at)
            SELECT rm.user_id, 'room_member_removed', r.id, r.name,
                   rm.removed_by, rm.removal_reason,
                   COALESCE(rm.removed_at, CURRENT_TIMESTAMP)
            FROM room_members rm
            JOIN rooms r ON r.id = rm.room_id
            WHERE rm.active = 0 AND rm.removed_at IS NOT NULL
            """
        )
        database.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (4, ?)",
            ("user_center_notifications",),
        )
        validate_v4_schema(database)
        violations = database.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"foreign key check failed during V4 migration: {violations}"
            )
        integrity = database.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(
                f"integrity check failed during V4 migration: {integrity}"
            )
        database.execute("PRAGMA user_version = 4")
        database.commit()
    except Exception:
        database.rollback()
        raise


def validate_v4_schema(database: sqlite3.Connection) -> None:
    validate_v3_schema(database)
    required = {
        "user_notifications": {
            "user_id",
            "kind",
            "room_id",
            "room_name",
            "actor_user_id",
            "reason",
            "created_at",
        }
    }
    missing = {
        table: sorted(columns - table_columns(database, table))
        for table, columns in required.items()
        if columns - table_columns(database, table)
    }
    if missing:
        raise sqlite3.DatabaseError(f"V4 schema validation failed: {missing}")


def migrate_legacy_database(database: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in database.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    database.commit()
    database.execute("PRAGMA foreign_keys = OFF")
    try:
        database.execute("BEGIN IMMEDIATE")
        database.execute(USERS_SQL)
        database.execute(ROOMS_SQL)
        database.execute(ROOM_MEMBERS_SQL)

        owner = database.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        owner_id = owner["id"] if owner else None
        cursor = database.execute(
            """
            INSERT INTO rooms
                (name, code, join_password_hash, owner_id, status,
                 score_min, score_max, members_locked, result_visibility)
            VALUES (?, 'LEGACY', ?, ?, 'scoring', 1, 5, 0, 'live')
            """,
            (
                "历史评审房间",
                generate_password_hash(secrets.token_urlsafe(24)),
                owner_id,
            ),
        )
        room_id = cursor.lastrowid
        users = database.execute("SELECT id FROM users ORDER BY id").fetchall()
        for index, user in enumerate(users):
            database.execute(
                """
                INSERT INTO room_members (room_id, user_id, role, active)
                VALUES (?, ?, ?, 1)
                """,
                (room_id, user["id"], "admin" if index == 0 else "judge"),
            )

        has_legacy_scores = "scores" in tables
        if has_legacy_scores:
            database.execute("ALTER TABLE scores RENAME TO legacy_scores")
        database.execute("ALTER TABLE candidates RENAME TO legacy_candidates")
        database.execute(CANDIDATES_SQL)
        database.execute(SCORES_SQL)
        database.execute(
            """
            INSERT INTO candidates
                (id, room_id, candidate_no, name, position, archived)
            SELECT id, ?, CAST(position AS TEXT), name, position, 0
            FROM legacy_candidates
            ORDER BY position
            """,
            (room_id,),
        )
        if has_legacy_scores:
            database.execute(
                """
                INSERT INTO scores
                    (room_id, user_id, candidate_id, score, updated_at)
                SELECT ?, user_id, candidate_id, score, updated_at
                FROM legacy_scores
                """,
                (room_id,),
            )
            database.execute("DROP TABLE legacy_scores")
        database.execute("DROP TABLE legacy_candidates")
        for statement in INDEX_STATEMENTS[:3]:
            database.execute(statement)
        database.execute("PRAGMA user_version = 2")
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.execute("PRAGMA foreign_keys = ON")


def claim_orphaned_legacy_room(database: sqlite3.Connection, user_id: int) -> None:
    room = database.execute(
        "SELECT id FROM rooms WHERE owner_id IS NULL AND code = 'LEGACY' LIMIT 1"
    ).fetchone()
    if room is None:
        return
    database.execute("UPDATE rooms SET owner_id = ? WHERE id = ?", (user_id, room["id"]))
    database.execute(
        """
        INSERT INTO room_members (room_id, user_id, role, active)
        VALUES (?, ?, 'admin', 1)
        ON CONFLICT(room_id, user_id)
        DO UPDATE SET role = 'admin', active = 1, can_score = 1
        """,
        (room["id"], user_id),
    )
    ensure_assignments_for_member(
        database, room["id"], user_id, include_existing=True
    )
    bump_roster_revision(database, room["id"])


def get_csrf_token() -> str:
    token = session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_valid_csrf() -> None:
    submitted = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    expected = session.get("csrf_token", "")
    if not submitted or not expected or not hmac.compare_digest(submitted, expected):
        abort(400, description="请求已过期，请刷新页面后重试。")


def safe_internal_return_path(value: str | None) -> str | None:
    """Accept only local GET routes as help-center return targets."""
    if not value or any(ord(character) < 32 for character in value):
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or parsed.path.startswith("//")
        or "\\" in parsed.path
    ):
        return None
    try:
        endpoint, _values = current_app.url_map.bind_to_environ(
            request.environ
        ).match(parsed.path, method="GET")
    except (MethodNotAllowed, NotFound):
        return None
    if endpoint in {"static", "help_center", "help_topic"}:
        return None
    return value


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(**kwargs)

    return wrapped_view


def require_room_member(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    room = database.execute(
        """
        SELECT r.*, rm.role AS member_role,
               CASE WHEN rm.can_score = 1 AND rm.scores_included = 1
                    THEN 1 ELSE 0 END AS member_can_score,
               rm.scores_included AS member_scores_included,
               CASE WHEN r.owner_id = rm.user_id THEN 1 ELSE 0 END AS is_owner
        FROM rooms r
        JOIN room_members rm ON rm.room_id = r.id
        WHERE r.id = ? AND rm.user_id = ? AND rm.active = 1
        """,
        (room_id, user_id),
    ).fetchone()
    if room is None:
        abort(404)
    return room


def require_room_admin(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    return require_room_manager(database, room_id, user_id)


def require_room_manager(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    room = require_room_member(database, room_id, user_id)
    if not room["is_owner"] and room["member_role"] != "admin":
        abort(403)
    return room


def require_room_owner(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> sqlite3.Row:
    room = require_room_member(database, room_id, user_id)
    if not room["is_owner"]:
        abort(403)
    return room


def room_results_visible(
    database: sqlite3.Connection, room: sqlite3.Row, user_id: int
) -> bool:
    if room["is_owner"] or room["member_role"] == "admin":
        return True
    if room["result_visibility"] == "live":
        return True
    if room["status"] == "closed":
        return True
    if room["result_visibility"] == "after_closed":
        return False
    if not room["member_can_score"]:
        return False
    required = database.execute(
        """
        SELECT COUNT(*)
        FROM review_assignments ra
        JOIN candidates c
          ON c.id = ra.candidate_id AND c.room_id = ra.room_id
        WHERE ra.room_id = ? AND ra.user_id = ?
          AND ra.status = 'required' AND c.archived = 0
        """,
        (room["id"], user_id),
    ).fetchone()[0]
    return required == 0


def build_room_state(
    database: sqlite3.Connection, room_id: int, user_id: int
) -> dict[str, Any]:
    room = require_room_member(database, room_id, user_id)
    results_visible = room_results_visible(database, room, user_id)
    ranking_rows = database.execute(
        """
        SELECT
            c.id, c.position, c.candidate_no, c.name, c.group_name,
            (SELECT COUNT(*)
             FROM review_assignments ra
             JOIN room_members rm
               ON rm.room_id = ra.room_id AND rm.user_id = ra.user_id
             WHERE ra.room_id = c.room_id AND ra.candidate_id = c.id
               AND ra.status != 'waived' AND rm.scores_included = 1
            ) AS expected_count,
            (SELECT COUNT(*)
             FROM scores s
             JOIN room_members rm
               ON rm.room_id = s.room_id AND rm.user_id = s.user_id
             WHERE s.room_id = c.room_id AND s.candidate_id = c.id
               AND rm.scores_included = 1
            ) AS score_count,
            (SELECT AVG(s.score)
             FROM scores s
             JOIN room_members rm
               ON rm.room_id = s.room_id AND rm.user_id = s.user_id
             WHERE s.room_id = c.room_id AND s.candidate_id = c.id
               AND rm.scores_included = 1
            ) AS avg_score,
            (SELECT COALESCE(SUM(s.score), 0)
             FROM scores s
             JOIN room_members rm
               ON rm.room_id = s.room_id AND rm.user_id = s.user_id
             WHERE s.room_id = c.room_id AND s.candidate_id = c.id
               AND rm.scores_included = 1
            ) AS total_score
        FROM candidates c
        WHERE c.room_id = ? AND c.archived = 0
        ORDER BY c.position, c.id
        """,
        (room_id,),
    ).fetchall()

    ordered_rows = list(ranking_rows)
    if results_visible:
        ordered_rows.sort(
            key=lambda row: (
                row["avg_score"] is None,
                -(float(row["avg_score"]) if row["avg_score"] is not None else 0),
                -row["score_count"],
                -row["total_score"],
                row["position"],
                row["id"],
            )
        )
    rankings = []
    previous_average: float | None = None
    for index, row in enumerate(ordered_rows, start=1):
        average = float(row["avg_score"]) if row["avg_score"] is not None else None
        if results_visible and average is not None:
            if previous_average is None or average != previous_average:
                rank = index
            else:
                rank = rankings[-1]["rank"]
            previous_average = average
        else:
            rank = None
        rankings.append(
            {
                "id": row["id"],
                "position": row["position"],
                "candidate_no": row["candidate_no"],
                "name": row["name"],
                "group_name": row["group_name"],
                "score_count": row["score_count"],
                "expected_count": row["expected_count"],
                "avg_score": round(average, 2)
                if results_visible and average is not None
                else None,
                "rank": rank,
            }
        )

    judge_rows = database.execute(
        """
        SELECT
            u.id, u.username, rm.role,
            CASE WHEN r.owner_id = u.id THEN 1 ELSE 0 END AS is_owner,
            (SELECT COUNT(*) FROM review_assignments ra
             JOIN candidates c
               ON c.id = ra.candidate_id AND c.room_id = ra.room_id
             WHERE ra.room_id = rm.room_id AND ra.user_id = u.id
               AND ra.status != 'waived' AND c.archived = 0) AS assigned_count,
            (SELECT COUNT(*) FROM review_assignments ra
             JOIN candidates c
               ON c.id = ra.candidate_id AND c.room_id = ra.room_id
             WHERE ra.room_id = rm.room_id AND ra.user_id = u.id
               AND ra.status = 'completed' AND c.archived = 0) AS score_count
        FROM room_members rm
        JOIN users u ON u.id = rm.user_id
        JOIN rooms r ON r.id = rm.room_id
        WHERE rm.room_id = ? AND rm.active = 1 AND rm.can_score = 1
          AND rm.scores_included = 1
        ORDER BY rm.joined_at, u.id
        """,
        (room_id,),
    ).fetchall()
    judges = [
        {
            "id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "is_owner": bool(row["is_owner"]),
            "assigned_count": row["assigned_count"],
            "score_count": row["score_count"],
            "is_current": row["id"] == user_id,
        }
        for row in judge_rows
    ]
    candidate_count = len(ranking_rows)
    current_judge = next((judge for judge in judges if judge["is_current"]), None)
    current_scored = current_judge["score_count"] if current_judge else 0
    current_assigned = current_judge["assigned_count"] if current_judge else 0
    totals = database.execute(
        """
        SELECT
            COUNT(CASE WHEN ra.status = 'completed' THEN 1 END) AS completed,
            COUNT(*) AS possible
        FROM review_assignments ra
        JOIN candidates c
          ON c.id = ra.candidate_id AND c.room_id = ra.room_id
        JOIN room_members rm
          ON rm.room_id = ra.room_id AND rm.user_id = ra.user_id
        WHERE ra.room_id = ? AND ra.status != 'waived'
          AND c.archived = 0 AND rm.scores_included = 1
        """,
        (room_id,),
    ).fetchone()
    total_scores = totals["completed"]
    judge_count = len(judges)

    candidate_rows = database.execute(
        """
        SELECT c.id, c.candidate_no, c.name, c.group_name, c.notes, c.position,
               s.score AS user_score, ra.status AS assignment_status
        FROM candidates c
        LEFT JOIN scores s
          ON s.room_id = c.room_id AND s.candidate_id = c.id AND s.user_id = ?
        LEFT JOIN review_assignments ra
          ON ra.room_id = c.room_id AND ra.candidate_id = c.id AND ra.user_id = ?
        WHERE c.room_id = ? AND c.archived = 0
        ORDER BY c.position, c.id
        """,
        (user_id, user_id, room_id),
    ).fetchall()

    return {
        "rankings": rankings,
        "judges": judges,
        "candidates": [dict(row) for row in candidate_rows],
        "judge_count": judge_count,
        "current_scored": current_scored,
        "current_assigned": current_assigned,
        "candidate_count": candidate_count,
        "total_scores": total_scores,
        "total_possible_scores": totals["possible"],
        "score_min": room["score_min"],
        "score_max": room["score_max"],
        "room_status": room["status"],
        "roster_revision": room["roster_revision"],
        "can_score": bool(
            room["status"] == "scoring" and room["member_can_score"]
        ),
        "results_visible": results_visible,
        "result_visibility": room["result_visibility"],
    }


def build_member_details(
    database: sqlite3.Connection, room_id: int, current_user_id: int
) -> list[dict[str, Any]]:
    member_rows = database.execute(
        """
        SELECT u.id, u.username, rm.role, rm.active, rm.can_score,
               rm.scores_included, rm.joined_at, rm.removed_at,
               rm.removal_reason,
               CASE WHEN r.owner_id = u.id THEN 1 ELSE 0 END AS is_owner
        FROM room_members rm
        JOIN users u ON u.id = rm.user_id
        JOIN rooms r ON r.id = rm.room_id
        WHERE rm.room_id = ?
        ORDER BY is_owner DESC, rm.active DESC, rm.joined_at, u.id
        """,
        (room_id,),
    ).fetchall()
    details = []
    for row in member_rows:
        counts = database.execute(
            """
            SELECT
                COUNT(CASE WHEN ra.status != 'waived' THEN 1 END) AS assigned_count,
                COUNT(CASE WHEN ra.status = 'completed' THEN 1 END) AS scored_count
            FROM review_assignments ra
            JOIN candidates c
              ON c.id = ra.candidate_id AND c.room_id = ra.room_id
            WHERE ra.room_id = ? AND ra.user_id = ? AND c.archived = 0
            """,
            (room_id, row["id"]),
        ).fetchone()
        missing_rows = database.execute(
            """
            SELECT c.name
            FROM review_assignments ra
            JOIN candidates c
              ON c.id = ra.candidate_id AND c.room_id = ra.room_id
            WHERE ra.room_id = ? AND ra.user_id = ?
              AND ra.status = 'required' AND c.archived = 0
            ORDER BY c.position, c.id
            """,
            (room_id, row["id"]),
        ).fetchall()
        details.append(
            {
                "id": row["id"],
                "username": row["username"],
                "role": row["role"],
                "active": bool(row["active"]),
                "can_score": bool(row["can_score"]),
                "scores_included": bool(row["scores_included"]),
                "is_owner": bool(row["is_owner"]),
                "is_current": row["id"] == current_user_id,
                "assigned_count": counts["assigned_count"],
                "scored_count": counts["scored_count"],
                "removed_at": row["removed_at"],
                "removal_reason": row["removal_reason"],
                "missing_names": [missing["name"] for missing in missing_rows],
            }
        )
    return details
