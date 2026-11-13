import csv
import io
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from werkzeug.security import generate_password_hash

from app import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": tmp_path / "test.db",
            "SECRET_KEY": "test-secret",
        }
    )


def csrf_from(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    assert match, response.get_data(as_text=True)[:500]
    return match.group(1).decode()


def register(client, username: str, password: str = "secret12"):
    token = csrf_from(client.get("/register"))
    return client.post(
        "/register",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "confirm_password": password,
        },
        follow_redirects=False,
    )


def login(client, username: str, password: str = "secret12"):
    token = csrf_from(client.get("/login"))
    return client.post(
        "/login",
        data={"csrf_token": token, "username": username, "password": password},
        follow_redirects=False,
    )


def create_room(
    client,
    name="第一面试间",
    password="roompass",
    scheme="5",
    score_min="0",
    score_max="20",
    result_visibility="live",
    owner_can_score="1",
):
    token = csrf_from(client.get("/rooms/create"))
    response = client.post(
        "/rooms/create",
        data={
            "csrf_token": token,
            "name": name,
            "join_password": password,
            "scheme": scheme,
            "score_min": score_min,
            "score_max": score_max,
            "result_visibility": result_visibility,
            "owner_can_score": owner_can_score,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    match = re.search(r"/rooms/(\d+)/settings", response.headers["Location"])
    assert match
    return int(match.group(1))


def add_candidate(client, room_id, name, candidate_no="", group_name=""):
    token = csrf_from(client.get(f"/rooms/{room_id}/candidates"))
    return client.post(
        f"/rooms/{room_id}/candidates",
        data={
            "csrf_token": token,
            "candidate_no": candidate_no,
            "name": name,
            "group_name": group_name,
            "notes": "",
            "position": "",
        },
        follow_redirects=False,
    )


def set_status(client, room_id, status):
    token = csrf_from(client.get(f"/rooms/{room_id}/settings"))
    return client.post(
        f"/rooms/{room_id}/status",
        data={"csrf_token": token, "status": status},
        follow_redirects=False,
    )


def join_room(client, code, password="roompass"):
    token = csrf_from(client.get("/rooms/join"))
    return client.post(
        "/rooms/join",
        data={"csrf_token": token, "code": code, "join_password": password},
        follow_redirects=False,
    )


def room_code(app, room_id):
    database = sqlite3.connect(app.config["DATABASE"])
    code = database.execute("SELECT code FROM rooms WHERE id = ?", (room_id,)).fetchone()[0]
    database.close()
    return code


def candidate_id(app, room_id, name):
    database = sqlite3.connect(app.config["DATABASE"])
    value = database.execute(
        "SELECT id FROM candidates WHERE room_id = ? AND name = ?", (room_id, name)
    ).fetchone()[0]
    database.close()
    return value


def user_id(app, username):
    database = sqlite3.connect(app.config["DATABASE"])
    value = database.execute(
        "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()[0]
    database.close()
    return value


def score(client, room_id, candidate, value):
    token = csrf_from(client.get(f"/rooms/{room_id}"))
    return client.post(
        f"/api/rooms/{room_id}/scores",
        json={"candidate_id": candidate, "score": value},
        headers={"X-CSRF-Token": token},
    )


def test_homepage_is_public_and_keeps_authenticated_workspace_entry(app):
    with app.test_client() as client:
        homepage = client.get("/")
        text = homepage.get_data(as_text=True)

        assert homepage.status_code == 200
        assert "多人协作评分与选拔工具" in text
        assert "让多人评分、进度核对和结果汇总保持在同一个地方。" in text
        assert 'href="/login"' in text
        assert 'href="/register"' in text

        register(client, "首页测试用户")
        authenticated_home = client.get("/", follow_redirects=False)

        assert authenticated_home.status_code == 302
        assert authenticated_home.headers["Location"].endswith("/rooms")


def test_registration_is_unlimited_and_users_can_join_rooms(app):
    for index in range(8):
        with app.test_client() as client:
            response = register(client, f"评委{index}")
            assert response.status_code == 302
            assert response.headers["Location"].endswith("/account/security")

    with app.test_client() as owner:
        login(owner, "评委0")
        room_id = create_room(owner)

    with app.test_client() as judge:
        login(judge, "评委7")
        response = join_room(judge, room_code(app, room_id))
        assert response.status_code == 302
        assert response.headers["Location"].endswith(f"/rooms/{room_id}")

    database = sqlite3.connect(app.config["DATABASE"])
    assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 8
    assert database.execute(
        "SELECT COUNT(*) FROM room_members WHERE room_id = ? AND active = 1", (room_id,)
    ).fetchone()[0] == 2
    database.close()


def test_rooms_are_isolated_and_support_different_scales(app):
    with app.test_client() as owner:
        register(owner, "管理员")
        room_ten = create_room(owner, name="十分制", scheme="10")
        add_candidate(owner, room_ten, "候选人甲", "A01")
        set_status(owner, room_ten, "scoring")

        room_hundred = create_room(owner, name="百分制", scheme="100")
        add_candidate(owner, room_hundred, "候选人乙", "B01")
        set_status(owner, room_hundred, "scoring")

        first_id = candidate_id(app, room_ten, "候选人甲")
        second_id = candidate_id(app, room_hundred, "候选人乙")
        assert score(owner, room_ten, first_id, 9).status_code == 200
        assert score(owner, room_hundred, second_id, 88).status_code == 200
        assert score(owner, room_ten, first_id, 11).status_code == 400
        assert score(owner, room_ten, first_id, 3.5).status_code == 400
        assert score(owner, room_ten, second_id, 8).status_code == 404

        first_state = owner.get(f"/api/rooms/{room_ten}/rankings").json
        second_state = owner.get(f"/api/rooms/{room_hundred}/rankings").json
        assert [item["name"] for item in first_state["rankings"]] == ["候选人甲"]
        assert first_state["rankings"][0]["avg_score"] == 9.0
        assert [item["name"] for item in second_state["rankings"]] == ["候选人乙"]
        assert second_state["rankings"][0]["avg_score"] == 88.0


def test_permissions_locking_and_room_status_are_enforced(app):
    with app.test_client() as owner:
        register(owner, "房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "候选人")
        candidate = candidate_id(app, room_id, "候选人")
        assert score(owner, room_id, candidate, 5).status_code == 409
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)

    with app.test_client() as outsider:
        register(outsider, "外部用户")
        assert outsider.get(f"/rooms/{room_id}").status_code == 404
        join_room(outsider, code)
        assert outsider.get(f"/rooms/{room_id}/settings").status_code == 403
        token = csrf_from(outsider.get(f"/rooms/{room_id}"))
        assert outsider.post(
            f"/rooms/{room_id}/candidates",
            data={"csrf_token": token, "name": "越权添加"},
        ).status_code == 403

    with app.test_client() as owner:
        login(owner, "房主")
        database = sqlite3.connect(app.config["DATABASE"])
        outsider_id = database.execute(
            "SELECT id FROM users WHERE username = '外部用户'"
        ).fetchone()[0]
        database.close()
        token = csrf_from(owner.get(f"/rooms/{room_id}/settings"))
        owner.post(
            f"/rooms/{room_id}/members/{outsider_id}/toggle",
            data={"csrf_token": token},
        )
        token = csrf_from(owner.get(f"/rooms/{room_id}/settings"))
        owner.post(
            f"/rooms/{room_id}/members-lock",
            data={"csrf_token": token},
        )

    with app.test_client() as inactive_judge:
        login(inactive_judge, "外部用户")
        token = csrf_from(inactive_judge.get("/rooms/join"))
        response = inactive_judge.post(
            "/rooms/join",
            data={"csrf_token": token, "code": code, "join_password": "roompass"},
            follow_redirects=True,
        )
        assert "成员资格已被管理员停用" in response.get_data(as_text=True)

    with app.test_client() as late_judge:
        register(late_judge, "迟到评委")
        token = csrf_from(late_judge.get("/rooms/join"))
        response = late_judge.post(
            "/rooms/join",
            data={"csrf_token": token, "code": code, "join_password": "roompass"},
            follow_redirects=True,
        )
        assert "锁定评委名单" in response.get_data(as_text=True)

    with app.test_client() as owner:
        login(owner, "房主")
        set_status(owner, room_id, "closed")
        assert score(owner, room_id, candidate, 4).status_code == 409


def test_concurrent_scores_and_revoke_are_preserved(app):
    usernames = [f"并发评委{index}" for index in range(6)]
    with app.test_client() as owner:
        register(owner, "并发房主")
        room_id = create_room(owner, scheme="10")
        add_candidate(owner, room_id, "并发候选人")
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)

    for username in usernames:
        with app.test_client() as client:
            register(client, username)
            assert join_room(client, code).status_code == 302

    candidate = candidate_id(app, room_id, "并发候选人")

    def submit(args):
        username, value = args
        with app.test_client() as client:
            login(client, username)
            return score(client, room_id, candidate, value).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(submit, zip(usernames, (5, 6, 7, 8, 9, 10))))
    assert statuses == [200] * 6

    with app.test_client() as client:
        login(client, usernames[0])
        dashboard = client.get(f"/rooms/{room_id}")
        token = csrf_from(dashboard)
        response = client.delete(
            f"/api/rooms/{room_id}/scores/{candidate}",
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200
        result = response.json["rankings"][0]
        assert result["score_count"] == 5
        assert result["avg_score"] == 8.0
        assert response.json["judge_count"] == 7


def test_candidate_archive_and_missing_review_details(app):
    with app.test_client() as owner:
        register(owner, "检查管理员")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "候选人一")
        add_candidate(owner, room_id, "候选人二")
        set_status(owner, room_id, "scoring")
        first_id = candidate_id(app, room_id, "候选人一")
        score(owner, room_id, first_id, 4)
        members = owner.get(f"/rooms/{room_id}/members")
        text = members.get_data(as_text=True)
        assert "漏评 1 人" in text
        assert "候选人二" in text

        token = csrf_from(owner.get(f"/rooms/{room_id}/candidates"))
        response = owner.post(
            f"/rooms/{room_id}/candidates/{first_id}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 302
        state = owner.get(f"/api/rooms/{room_id}/rankings").json
        assert [item["name"] for item in state["rankings"]] == ["候选人二"]


def test_legacy_database_is_migrated_without_data_loss(tmp_path):
    database_path = tmp_path / "legacy.db"
    database = sqlite3.connect(database_path)
    database.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY,
            position INTEGER NOT NULL UNIQUE,
            name TEXT NOT NULL UNIQUE
        );
        CREATE TABLE scores (
            user_id INTEGER NOT NULL,
            candidate_id INTEGER NOT NULL,
            score INTEGER NOT NULL CHECK (score BETWEEN 1 AND 5),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, candidate_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
        );
        CREATE INDEX idx_scores_candidate_id ON scores(candidate_id);
        """
    )
    database.execute(
        "INSERT INTO users (id, username, password_hash) VALUES (1, '旧评委', ?)",
        (generate_password_hash("secret12"),),
    )
    database.execute("INSERT INTO candidates VALUES (1, 1, '旧候选人')")
    database.execute("INSERT INTO scores (user_id, candidate_id, score) VALUES (1, 1, 5)")
    database.commit()
    database.close()

    migrated_app = create_app(
        {"TESTING": True, "DATABASE": database_path, "SECRET_KEY": "legacy-test"}
    )
    database = sqlite3.connect(database_path)
    database.row_factory = sqlite3.Row
    room = database.execute("SELECT * FROM rooms").fetchone()
    assert room["name"] == "历史评审房间"
    assert room["score_min"] == 1 and room["score_max"] == 5
    assert database.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
    assert database.execute("SELECT score FROM scores").fetchone()[0] == 5
    assert database.execute("PRAGMA user_version").fetchone()[0] == 4
    assert database.execute("SELECT COUNT(*) FROM review_assignments").fetchone()[0] == 1
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    database.close()

    with migrated_app.test_client() as client:
        assert login(client, "旧评委").status_code == 302
        page = client.get(f"/rooms/{room['id']}")
        assert page.status_code == 200
        assert "旧候选人" in page.get_data(as_text=True)


def test_owner_removal_preserves_completed_scores_and_waives_pending(app):
    with app.test_client() as owner:
        register(owner, "保留房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "甲")
        add_candidate(owner, room_id, "乙")
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)

    with app.test_client() as judge:
        register(judge, "被移出评委")
        join_room(judge, code)
        assert score(judge, room_id, candidate_id(app, room_id, "甲"), 5).status_code == 200

    with app.test_client() as owner:
        login(owner, "保留房主")
        target_id = user_id(app, "被移出评委")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        response = owner.post(
            f"/rooms/{room_id}/members/{target_id}/toggle",
            data={"csrf_token": token, "reason": "测试移出"},
        )
        assert response.status_code == 302
        state = owner.get(f"/api/rooms/{room_id}/rankings").json
        row = next(item for item in state["rankings"] if item["name"] == "甲")
        assert row["score_count"] == 1
        assert row["avg_score"] == 5.0

    database = sqlite3.connect(app.config["DATABASE"])
    statuses = dict(
        database.execute(
            """
            SELECT c.name, ra.status FROM review_assignments ra
            JOIN candidates c ON c.id = ra.candidate_id
            WHERE ra.room_id = ? AND ra.user_id = ?
            """,
            (room_id, target_id),
        ).fetchall()
    )
    assert statuses == {"甲": "completed", "乙": "waived"}
    assert database.execute(
        "SELECT scores_included FROM room_members WHERE room_id = ? AND user_id = ?",
        (room_id, target_id),
    ).fetchone()[0] == 1
    database.close()

    with app.test_client() as removed:
        login(removed, "被移出评委")
        assert removed.get(f"/rooms/{room_id}").status_code == 404


def test_removed_member_receives_persistent_notification_with_reason(app):
    with app.test_client() as owner:
        register(owner, "通知房主")
        room_id = create_room(owner, name="通知测试房间")
        code = room_code(app, room_id)

    with app.test_client() as member:
        register(member, "通知成员")
        join_room(member, code)

    target_id = user_id(app, "通知成员")
    with app.test_client() as owner:
        login(owner, "通知房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        response = owner.post(
            f"/rooms/{room_id}/members/{target_id}/toggle",
            data={"csrf_token": token, "reason": "本轮评审名单已调整"},
        )
        assert response.status_code == 302
        assert "本轮评审名单已调整" not in owner.get(
            "/user/notifications"
        ).get_data(as_text=True)

    with app.test_client() as member:
        login(member, "通知成员")
        page = member.get("/user/notifications")
        text = page.get_data(as_text=True)
        assert page.status_code == 200
        assert "你已被移出房间“通知测试房间”" in text
        assert "移出理由：本轮评审名单已调整" in text
        assert "通知房主" in text
        assert member.get(f"/rooms/{room_id}").status_code == 404

    with app.test_client() as owner:
        login(owner, "通知房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{target_id}/toggle",
            data={"csrf_token": token},
        )

    with app.test_client() as member:
        login(member, "通知成员")
        assert "本轮评审名单已调整" in member.get(
            "/user/notifications"
        ).get_data(as_text=True)


def test_only_owner_can_manage_members_and_transfer_ownership(app):
    with app.test_client() as owner:
        register(owner, "权限房主")
        room_id = create_room(owner)
    with app.test_client() as manager:
        register(manager, "协管员")
    with app.test_client() as member:
        register(member, "普通成员")

    manager_id = user_id(app, "协管员")
    member_id = user_id(app, "普通成员")
    with app.test_client() as owner:
        login(owner, "权限房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/add",
            data={"csrf_token": token, "username": "协管员", "role": "admin", "can_score": "1"},
        )
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/add",
            data={"csrf_token": token, "username": "普通成员", "role": "judge", "can_score": "1"},
        )
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        assert owner.post(
            f"/rooms/{room_id}/members/{member_id}/role",
            data={"csrf_token": token, "role": "admin", "can_score": "1"},
        ).status_code == 302
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        assert owner.post(
            f"/rooms/{room_id}/members/{member_id}/role",
            data={"csrf_token": token, "role": "judge", "can_score": "1"},
        ).status_code == 302

    with app.test_client() as manager:
        login(manager, "协管员")
        members_page = manager.get(f"/rooms/{room_id}/members")
        assert members_page.status_code == 200
        token = csrf_from(members_page)
        assert manager.post(
            f"/rooms/{room_id}/members/{member_id}/toggle",
            data={"csrf_token": token},
        ).status_code == 403
        assert manager.post(
            f"/rooms/{room_id}/invitations",
            data={"csrf_token": token, "role": "judge", "max_uses": "1", "expiry": "7"},
        ).status_code == 403
        assert manager.post(
            f"/rooms/{room_id}/owner-transfer",
            data={"csrf_token": token, "user_id": member_id},
        ).status_code == 403
        # A co-manager may still maintain the candidate roster.
        token = csrf_from(manager.get(f"/rooms/{room_id}/candidates"))
        assert manager.post(
            f"/rooms/{room_id}/candidates",
            data={"csrf_token": token, "name": "协管添加", "position": ""},
        ).status_code == 302

    assert manager_id != member_id


def test_owner_and_manager_badges_follow_actual_room_ownership(app):
    with app.test_client() as owner:
        register(owner, "角标房主")
        room_id = create_room(owner)
    with app.test_client() as manager:
        register(manager, "角标协管")

    with app.test_client() as owner:
        login(owner, "角标房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/add",
            data={
                "csrf_token": token,
                "username": "角标协管",
                "role": "admin",
                "can_score": "1",
            },
        )
        state = owner.get(f"/api/rooms/{room_id}/rankings").json
        judges = {judge["username"]: judge for judge in state["judges"]}
        assert judges["角标房主"]["is_owner"] is True
        assert judges["角标协管"]["is_owner"] is False
        assert judges["角标协管"]["role"] == "admin"

        html = owner.get(f"/rooms/{room_id}").get_data(as_text=True)
        assert re.search(
            r"<strong>角标房主</strong>.*?<small>房主</small>", html
        )
        assert re.search(
            r"<strong>角标协管</strong>.*?<small>协管员</small>", html
        )
        script = owner.get("/static/app.js").get_data(as_text=True)
        assert "if (judge.is_owner)" in script
        assert 'makeElement("small", "", "房主")' in script


def test_dynamic_candidate_revision_and_assignment_matrix(app):
    with app.test_client() as owner:
        register(owner, "动态房主")
        room_id = create_room(owner)
        for index in range(10):
            add_candidate(owner, room_id, f"候选人{index + 1}")
        set_status(owner, room_id, "scoring")
        before = owner.get(f"/api/rooms/{room_id}/rankings").json
        assert before["candidate_count"] == 10
        assert before["current_assigned"] == 10
        revision = before["roster_revision"]
        add_candidate(owner, room_id, "候选人11")
        after = owner.get(f"/api/rooms/{room_id}/rankings").json
        assert after["candidate_count"] == 11
        assert after["current_assigned"] == 11
        assert after["roster_revision"] == revision + 1
        assert after["candidates"][-1]["name"] == "候选人11"
        assert after["candidates"][-1]["assignment_status"] == "required"


def test_single_use_invitation_is_atomic_under_concurrency(app):
    with app.test_client() as owner:
        register(owner, "邀请房主")
        room_id = create_room(owner)
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        response = owner.post(
            f"/rooms/{room_id}/invitations",
            data={
                "csrf_token": token,
                "role": "judge",
                "can_score": "1",
                "assign_existing": "1",
                "expiry": "7",
                "max_uses": "1",
            },
        )
        assert response.status_code == 302
        with owner.session_transaction() as saved_session:
            invitation_token = saved_session["new_invite"]["token"]

    for username in ("邀请用户一", "邀请用户二"):
        with app.test_client() as client:
            register(client, username)

    def accept(username):
        with app.test_client() as client:
            login(client, username)
            page = client.get(f"/invite/{invitation_token}")
            csrf = csrf_from(page)
            return client.post(
                f"/invite/{invitation_token}",
                data={"csrf_token": csrf},
                follow_redirects=False,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(accept, ("邀请用户一", "邀请用户二")))
    assert statuses.count(302) >= 1
    assert all(status in {302, 410} for status in statuses)

    database = sqlite3.connect(app.config["DATABASE"])
    joined = database.execute(
        "SELECT COUNT(*) FROM room_members WHERE room_id = ? AND user_id != (SELECT owner_id FROM rooms WHERE id = ?)",
        (room_id, room_id),
    ).fetchone()[0]
    invitation = database.execute(
        "SELECT used_count, max_uses FROM room_invitations WHERE room_id = ?",
        (room_id,),
    ).fetchone()
    assert joined == 1
    assert invitation == (1, 1)
    database.close()


def test_result_visibility_is_enforced_server_side(app):
    with app.test_client() as owner:
        register(owner, "隐藏结果房主")
        room_id = create_room(owner, result_visibility="after_complete")
        add_candidate(owner, room_id, "隐藏候选人")
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)

    with app.test_client() as judge:
        register(judge, "隐藏结果评委")
        join_room(judge, code)
        hidden = judge.get(f"/api/rooms/{room_id}/rankings").json
        assert hidden["results_visible"] is False
        assert hidden["rankings"][0]["avg_score"] is None
        assert hidden["rankings"][0]["rank"] is None
        html = judge.get(f"/rooms/{room_id}/results").get_data(as_text=True)
        assert "结果尚未开放" in html
        assert score(judge, room_id, candidate_id(app, room_id, "隐藏候选人"), 4).status_code == 200
        visible = judge.get(f"/api/rooms/{room_id}/rankings").json
        assert visible["results_visible"] is True
        assert visible["rankings"][0]["avg_score"] == 4.0


def test_all_visible_room_members_can_export_results_csv(app):
    with app.test_client() as owner:
        register(owner, "导出房主")
        room_id = create_room(owner, result_visibility="live")
        add_candidate(
            owner,
            room_id,
            "导出候选人",
            candidate_no="=1+1",
            group_name="产品组",
        )
        set_status(owner, room_id, "scoring")
        target_id = candidate_id(app, room_id, "导出候选人")
        assert score(owner, room_id, target_id, 5).status_code == 200
        code = room_code(app, room_id)

    with app.test_client() as member:
        register(member, "导出观察成员")
        assert join_room(member, code).status_code == 302

    with app.test_client() as owner:
        login(owner, "导出房主")
        observer_id = user_id(app, "导出观察成员")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        response = owner.post(
            f"/rooms/{room_id}/members/{observer_id}/role",
            data={"csrf_token": token, "role": "judge", "can_score": "0"},
        )
        assert response.status_code == 302
        results_page = owner.get(f"/rooms/{room_id}/results")
        assert f'/rooms/{room_id}/results/export.csv' in results_page.get_data(
            as_text=True
        )
        assert owner.get(f"/rooms/{room_id}/results/export.csv").status_code == 200

    with app.test_client() as member:
        login(member, "导出观察成员")
        response = member.get(f"/rooms/{room_id}/results/export.csv")
        assert response.status_code == 200
        assert response.data.startswith(b"\xef\xbb\xbf")
        assert response.mimetype == "text/csv"
        assert response.headers["Cache-Control"] == "no-store"
        assert "attachment" in response.headers["Content-Disposition"]
        assert f"hengxuan-{code}-results.csv" in response.headers[
            "Content-Disposition"
        ]

        rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
        assert rows == [
            [
                "排名",
                "候选人编号",
                "候选人姓名",
                "分组",
                "平均分",
                "有效评分数",
                "应评分数",
                "评分状态",
            ],
            ["1", "'=1+1", "导出候选人", "产品组", "5.00", "1", "1", "已完成"],
        ]

    with app.test_client() as outsider:
        register(outsider, "导出房外用户")
        assert outsider.get(f"/rooms/{room_id}/results/export.csv").status_code == 404


def test_result_export_obeys_visibility_rule(app):
    with app.test_client() as owner:
        register(owner, "受限导出房主")
        room_id = create_room(owner, result_visibility="after_complete")
        add_candidate(owner, room_id, "受限导出候选人")
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)

    with app.test_client() as judge:
        register(judge, "受限导出评委")
        join_room(judge, code)
        export_url = f"/rooms/{room_id}/results/export.csv"
        assert judge.get(export_url).status_code == 403
        locked_page = judge.get(f"/rooms/{room_id}/results").get_data(as_text=True)
        assert export_url not in locked_page

        target_id = candidate_id(app, room_id, "受限导出候选人")
        assert score(judge, room_id, target_id, 4).status_code == 200
        assert judge.get(export_url).status_code == 200


def test_recovery_code_is_one_time_and_revokes_old_sessions(app):
    with app.test_client() as primary:
        response = register(primary, "找回用户")
        security = primary.get(response.headers["Location"])
        match = re.search(rb"<code>([^<]+)</code>", security.data)
        assert match
        recovery_code = match.group(1).decode()

    old_session = app.test_client()
    assert login(old_session, "找回用户").status_code == 302
    assert old_session.get("/rooms").status_code == 200

    with app.test_client() as recovery:
        token = csrf_from(recovery.get("/forgot-password"))
        response = recovery.post(
            "/forgot-password",
            data={
                "csrf_token": token,
                "username": "找回用户",
                "recovery_code": recovery_code,
                "password": "newsecret12",
                "confirm_password": "newsecret12",
            },
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/account/security")

    assert old_session.get("/rooms").status_code == 302
    with app.test_client() as replay:
        token = csrf_from(replay.get("/forgot-password"))
        response = replay.post(
            "/forgot-password",
            data={
                "csrf_token": token,
                "username": "找回用户",
                "recovery_code": recovery_code,
                "password": "attackerpass",
                "confirm_password": "attackerpass",
            },
            follow_redirects=True,
        )
        assert "账号信息或恢复凭证不正确" in response.get_data(as_text=True)
        assert login(replay, "找回用户", "newsecret12").status_code == 302


def test_v2_upgrade_preserves_counts_and_is_idempotent(tmp_path):
    database_path = tmp_path / "v2.db"
    database = sqlite3.connect(database_path)
    database.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE rooms (id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE COLLATE NOCASE NOT NULL, join_password_hash TEXT NOT NULL, owner_id INTEGER, status TEXT NOT NULL DEFAULT 'draft', score_min INTEGER NOT NULL DEFAULT 1, score_max INTEGER NOT NULL DEFAULT 5, members_locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE room_members (room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'judge', active INTEGER NOT NULL DEFAULT 1, joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,user_id));
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL, candidate_no TEXT, name TEXT NOT NULL, group_name TEXT, notes TEXT, position INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,room_id));
        CREATE TABLE scores (room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, score INTEGER NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,user_id,candidate_id));
        PRAGMA user_version = 2;
        """
    )
    for user in range(1, 4):
        database.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
            (user, f"旧用户{user}", generate_password_hash("secret12")),
        )
    database.execute(
        "INSERT INTO rooms (id,name,code,join_password_hash,owner_id,status) VALUES (1,'旧房间','OLDROOM',?,1,'scoring')",
        (generate_password_hash("roompass"),),
    )
    for user in range(1, 4):
        database.execute(
            "INSERT INTO room_members (room_id,user_id,role) VALUES (1,?,?)",
            (user, "admin" if user == 1 else "judge"),
        )
    for candidate in range(1, 13):
        database.execute(
            "INSERT INTO candidates (id,room_id,name,position) VALUES (?,1,?,?)",
            (candidate, f"旧候选人{candidate}", candidate),
        )
        for user in range(1, 4):
            database.execute(
                "INSERT INTO scores VALUES (1,?,?,5,CURRENT_TIMESTAMP)",
                (user, candidate),
            )
    database.commit()
    database.close()

    config = {"TESTING": True, "DATABASE": database_path, "SECRET_KEY": "v2-test"}
    create_app(config)
    create_app(config)
    database = sqlite3.connect(database_path)
    assert database.execute("PRAGMA user_version").fetchone()[0] == 4
    assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    assert database.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 12
    assert database.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 36
    assert database.execute("SELECT COUNT(*) FROM review_assignments").fetchone()[0] == 36
    assert database.execute("SELECT result_visibility FROM rooms").fetchone()[0] == "live"
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []
    database.close()


def test_future_only_member_stays_unassigned_after_room_reopens(app):
    with app.test_client() as owner:
        register(owner, "后续任务房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "已有一")
        add_candidate(owner, room_id, "已有二")
        set_status(owner, room_id, "scoring")
    with app.test_client() as future_judge:
        register(future_judge, "后续评委")

    with app.test_client() as owner:
        login(owner, "后续任务房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/add",
            data={
                "csrf_token": token,
                "username": "后续评委",
                "role": "judge",
                "can_score": "1",
                # assign_existing intentionally omitted
            },
        )
        set_status(owner, room_id, "closed")
        set_status(owner, room_id, "scoring")

    with app.test_client() as future_judge:
        login(future_judge, "后续评委")
        state = future_judge.get(f"/api/rooms/{room_id}/rankings").json
        assert state["current_assigned"] == 0
        assert all(item["assignment_status"] is None for item in state["candidates"])

    with app.test_client() as owner:
        login(owner, "后续任务房主")
        add_candidate(owner, room_id, "后续新增")
    with app.test_client() as future_judge:
        login(future_judge, "后续评委")
        state = future_judge.get(f"/api/rooms/{room_id}/rankings").json
        assert state["current_assigned"] == 1
        assigned = [
            item["name"] for item in state["candidates"]
            if item["assignment_status"] == "required"
        ]
        assert assigned == ["后续新增"]


def test_excluded_scores_do_not_return_silently_when_member_is_restored(app):
    with app.test_client() as owner:
        register(owner, "作废房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "作废候选人")
        set_status(owner, room_id, "scoring")
        code = room_code(app, room_id)
    with app.test_client() as judge:
        register(judge, "作废评委")
        join_room(judge, code)
        score(judge, room_id, candidate_id(app, room_id, "作废候选人"), 5)

    target_id = user_id(app, "作废评委")
    with app.test_client() as owner:
        login(owner, "作废房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{target_id}/toggle",
            data={"csrf_token": token},
        )
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{target_id}/score-inclusion",
            data={"csrf_token": token, "include": "0", "reason": "评分异常"},
        )
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{target_id}/toggle",
            data={"csrf_token": token},
        )
        state = owner.get(f"/api/rooms/{room_id}/rankings").json
        assert state["rankings"][0]["score_count"] == 0
        database = sqlite3.connect(app.config["DATABASE"])
        assert database.execute(
            "SELECT active, scores_included FROM room_members WHERE room_id=? AND user_id=?",
            (room_id, target_id),
        ).fetchone() == (1, 0)
        database.close()

        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{target_id}/score-inclusion",
            data={"csrf_token": token, "include": "1"},
        )
        state = owner.get(f"/api/rooms/{room_id}/rankings").json
        assert state["rankings"][0]["score_count"] == 1
        assert state["rankings"][0]["avg_score"] == 5.0


def test_stale_manager_and_closed_candidate_requests_are_rechecked(app):
    with app.test_client() as owner:
        register(owner, "竞态房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "初始候选人")
        set_status(owner, room_id, "scoring")
    with app.test_client() as manager:
        register(manager, "竞态协管")
    manager_id = user_id(app, "竞态协管")

    with app.test_client() as owner:
        login(owner, "竞态房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/add",
            data={"csrf_token": token, "username": "竞态协管", "role": "admin", "can_score": "1"},
        )

    manager = app.test_client()
    login(manager, "竞态协管")
    stale_token = csrf_from(manager.get(f"/rooms/{room_id}/candidates"))
    with app.test_client() as owner:
        login(owner, "竞态房主")
        token = csrf_from(owner.get(f"/rooms/{room_id}/members"))
        owner.post(
            f"/rooms/{room_id}/members/{manager_id}/toggle",
            data={"csrf_token": token},
        )
    response = manager.post(
        f"/rooms/{room_id}/candidates",
        data={"csrf_token": stale_token, "name": "越权候选人", "position": ""},
    )
    assert response.status_code == 404

    with app.test_client() as owner:
        login(owner, "竞态房主")
        stale_token = csrf_from(owner.get(f"/rooms/{room_id}/candidates"))
        set_status(owner, room_id, "closed")
        response = owner.post(
            f"/rooms/{room_id}/candidates",
            data={"csrf_token": stale_token, "name": "未确认重开", "position": ""},
        )
        assert response.status_code == 302
        database = sqlite3.connect(app.config["DATABASE"])
        assert database.execute(
            "SELECT COUNT(*) FROM candidates WHERE room_id=? AND name='未确认重开'",
            (room_id,),
        ).fetchone()[0] == 0
        assert database.execute(
            "SELECT status FROM rooms WHERE id=?", (room_id,)
        ).fetchone()[0] == "closed"
        database.close()


def test_stale_scheme_form_cannot_change_scale_after_first_score(app):
    with app.test_client() as owner:
        register(owner, "分制竞态房主")
        room_id = create_room(owner, scheme="5")
        add_candidate(owner, room_id, "分制候选人")
        set_status(owner, room_id, "scoring")
        stale_token = csrf_from(owner.get(f"/rooms/{room_id}/settings"))
        assert score(owner, room_id, candidate_id(app, room_id, "分制候选人"), 5).status_code == 200
        response = owner.post(
            f"/rooms/{room_id}/settings/scheme",
            data={
                "csrf_token": stale_token,
                "scheme": "10",
                "score_min": "1",
                "score_max": "10",
            },
        )
        assert response.status_code == 302
    database = sqlite3.connect(app.config["DATABASE"])
    assert database.execute(
        "SELECT score_min, score_max FROM rooms WHERE id=?", (room_id,)
    ).fetchone() == (1, 5)
    assert database.execute("SELECT score FROM scores WHERE room_id=?", (room_id,)).fetchone()[0] == 5
    database.close()


def test_public_registration_cannot_claim_platform_admin_name(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": tmp_path / "admin-name.db",
            "SECRET_KEY": "admin-name-test",
            "PLATFORM_ADMIN_USERNAMES": {"预留管理员"},
            "PLATFORM_ADMIN_USER_IDS": set(),
        }
    )
    with app.test_client() as client:
        register(client, "预留管理员")
        token = csrf_from(client.get("/account/security"))
        response = client.post(
            "/admin/password-resets",
            data={"csrf_token": token, "username": "预留管理员"},
        )
        assert response.status_code == 403
    database = sqlite3.connect(app.config["DATABASE"])
    assert database.execute("SELECT is_platform_admin FROM users").fetchone()[0] == 0
    database.close()


def test_password_change_invalidates_all_operator_reset_codes(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": tmp_path / "operator-reset.db",
            "SECRET_KEY": "operator-reset-test",
            "PLATFORM_ADMIN_USER_IDS": {1},
        }
    )
    with app.test_client() as admin:
        register(admin, "平台协助员")
    with app.test_client() as target:
        register(target, "重置目标")

    issued_codes = []
    with app.test_client() as admin:
        login(admin, "平台协助员")
        for _ in range(2):
            token = csrf_from(admin.get("/account/security"))
            response = admin.post(
                "/admin/password-resets",
                data={"csrf_token": token, "username": "重置目标"},
            )
            assert response.status_code == 302
            with admin.session_transaction() as saved_session:
                issued_codes.append(saved_session["new_admin_reset"]["code"])

    with app.test_client() as target:
        login(target, "重置目标")
        token = csrf_from(target.get("/account/security"))
        response = target.post(
            "/account/security/password",
            data={
                "csrf_token": token,
                "current_password": "secret12",
                "password": "changedpass12",
                "confirm_password": "changedpass12",
            },
        )
        assert response.status_code == 302

    database = sqlite3.connect(app.config["DATABASE"])
    assert database.execute(
        "SELECT COUNT(*) FROM password_reset_tokens WHERE used_at IS NULL"
    ).fetchone()[0] == 0
    database.close()

    with app.test_client() as attacker:
        token = csrf_from(attacker.get("/forgot-password"))
        response = attacker.post(
            "/forgot-password",
            data={
                "csrf_token": token,
                "username": "重置目标",
                "recovery_code": issued_codes[0],
                "password": "attackerpass",
                "confirm_password": "attackerpass",
            },
            follow_redirects=True,
        )
        assert "账号信息或恢复凭证不正确" in response.get_data(as_text=True)
        assert login(attacker, "重置目标", "changedpass12").status_code == 302


def test_invalid_v2_database_does_not_get_marked_as_migrated(tmp_path):
    database_path = tmp_path / "broken-v2.db"
    database = sqlite3.connect(database_path)
    database.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE rooms (id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL, join_password_hash TEXT NOT NULL, owner_id INTEGER, status TEXT NOT NULL DEFAULT 'draft', score_min INTEGER NOT NULL DEFAULT 1, score_max INTEGER NOT NULL DEFAULT 5, members_locked INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(owner_id) REFERENCES users(id));
        CREATE TABLE room_members (room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, role TEXT NOT NULL DEFAULT 'judge', active INTEGER NOT NULL DEFAULT 1, joined_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,user_id), FOREIGN KEY(room_id) REFERENCES rooms(id), FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE candidates (id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL, candidate_no TEXT, name TEXT NOT NULL, group_name TEXT, notes TEXT, position INTEGER NOT NULL DEFAULT 1, archived INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,room_id), FOREIGN KEY(room_id) REFERENCES rooms(id));
        CREATE TABLE scores (room_id INTEGER NOT NULL, user_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, score INTEGER NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_id,user_id,candidate_id), FOREIGN KEY(room_id,user_id) REFERENCES room_members(room_id,user_id), FOREIGN KEY(candidate_id,room_id) REFERENCES candidates(id,room_id));
        INSERT INTO users (id,username,password_hash) VALUES (1,'合法用户','hash');
        INSERT INTO rooms (id,name,code,join_password_hash,owner_id) VALUES (1,'损坏房间','BROKEN','hash',1);
        INSERT INTO room_members (room_id,user_id,role) VALUES (1,1,'admin');
        INSERT INTO candidates (id,room_id,name,position) VALUES (1,1,'候选人',1);
        INSERT INTO scores (room_id,user_id,candidate_id,score) VALUES (1,999,1,5);
        PRAGMA user_version = 2;
        """
    )
    database.commit()
    database.close()

    with pytest.raises(sqlite3.IntegrityError):
        create_app(
            {"TESTING": True, "DATABASE": database_path, "SECRET_KEY": "broken"}
        )

    database = sqlite3.connect(database_path)
    assert database.execute("PRAGMA user_version").fetchone()[0] == 2
    columns = {row[1] for row in database.execute("PRAGMA table_info(users)")}
    assert "session_version" not in columns
    assert database.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 1
    database.close()


def link_for_text(response, text):
    html = response.get_data(as_text=True)
    for href, body in re.findall(
        r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL
    ):
        label = unescape(re.sub(r"<[^>]+>", "", body)).strip()
        if text in label:
            return unescape(href)
    raise AssertionError(f"没有找到链接：{text}")


def test_username_opens_user_center_with_three_destinations(app):
    with app.test_client() as anonymous:
        for path in ("/user", "/user/notifications", "/user/settings"):
            response = anonymous.get(path, follow_redirects=False)
            assert response.status_code == 302
            assert response.headers["Location"].endswith("/login")

    with app.test_client() as client:
        registration = register(client, "中心用户")
        client.get(registration.headers["Location"])

        rooms_page = client.get("/rooms")
        assert link_for_text(rooms_page, "中心用户") == "/user"
        assert 'aria-label="进入用户中心：中心用户"' in rooms_page.get_data(
            as_text=True
        )

        center = client.get("/user")
        center_text = center.get_data(as_text=True)
        assert center.status_code == 200
        assert "用户中心" in center_text
        assert link_for_text(center, "通知") == "/user/notifications"
        assert link_for_text(center, "账号与安全") == "/account/security"
        assert link_for_text(center, "查看开发状态") == "/user/settings"

        settings = client.get("/user/settings")
        assert "该功能开发中" in settings.get_data(as_text=True)
        security = client.get("/account/security")
        assert link_for_text(security, "返回用户中心") == "/user"


def test_v3_database_migrates_removal_notifications(tmp_path):
    database_path = tmp_path / "v3-notifications.db"
    old_app = create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "v3-notifications",
        }
    )
    with old_app.test_client() as owner:
        register(owner, "迁移通知房主")
        room_id = create_room(owner, name="迁移通知房间")
    with old_app.test_client() as member:
        register(member, "迁移通知成员")
        join_room(member, room_code(old_app, room_id))

    target_id = user_id(old_app, "迁移通知成员")
    actor_id = user_id(old_app, "迁移通知房主")
    database = sqlite3.connect(database_path)
    database.execute("DROP TABLE user_notifications")
    database.execute(
        """
        UPDATE room_members
        SET active = 0, removed_at = '2026-08-31 12:00:00',
            removed_by = ?, removal_reason = '历史移出原因'
        WHERE room_id = ? AND user_id = ?
        """,
        (actor_id, room_id, target_id),
    )
    database.execute("PRAGMA user_version = 3")
    database.commit()
    database.close()

    migrated_app = create_app(
        {
            "TESTING": True,
            "DATABASE": database_path,
            "SECRET_KEY": "v4-notifications",
        }
    )
    database = sqlite3.connect(database_path)
    assert database.execute("PRAGMA user_version").fetchone()[0] == 4
    notification = database.execute(
        """
        SELECT kind, room_name, actor_user_id, reason, created_at
        FROM user_notifications WHERE user_id = ?
        """,
        (target_id,),
    ).fetchone()
    assert notification == (
        "room_member_removed",
        "迁移通知房间",
        actor_id,
        "历史移出原因",
        "2026-08-31 12:00:00",
    )
    database.close()

    with migrated_app.test_client() as member:
        login(member, "迁移通知成员")
        assert "历史移出原因" in member.get(
            "/user/notifications"
        ).get_data(as_text=True)


def test_help_center_is_authenticated_and_available_to_owner_and_judge(app):
    with app.test_client() as anonymous:
        response = anonymous.get("/help", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")

    with app.test_client() as owner:
        register(owner, "帮助房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "帮助候选人")
        code = room_code(app, room_id)
        response = owner.get("/help")
        assert response.status_code == 200
        assert "帮助中心" in response.get_data(as_text=True)

    with app.test_client() as judge:
        register(judge, "帮助评委")
        join_room(judge, code)
        response = judge.get("/help/results-ranking")
        text = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "结果与排名" in text
        assert "房主和协管员始终可以查看结果" in text


def test_global_help_entry_keeps_each_main_room_page_as_return_target(app):
    with app.test_client() as owner:
        register(owner, "返回房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "返回候选人")
        paths = (
            f"/rooms/{room_id}/candidates",
            f"/rooms/{room_id}",
            f"/rooms/{room_id}/members",
            f"/rooms/{room_id}/results",
            f"/rooms/{room_id}/settings",
        )
        for path in paths:
            page = owner.get(path)
            text = page.get_data(as_text=True)
            assert page.status_code == 200
            assert 'aria-label="帮助"' in text
            assert '<span aria-hidden="true">？</span>' not in text
            help_link = link_for_text(page, "帮助")
            parsed = urlsplit(help_link)
            assert parsed.path == "/help"
            assert parse_qs(parsed.query)["return_to"] == [path]

            help_page = owner.get(help_link)
            assert help_page.status_code == 200
            back_link = link_for_text(help_page, "返回上一页")
            assert back_link == path


@pytest.mark.parametrize(
    "return_to",
    (
        "https://example.com/steal",
        "//example.com/steal",
        "javascript:alert(1)",
        "/not-a-real-page",
        "/help/results-ranking",
        "/\\example.com/steal",
    ),
)
def test_help_return_target_rejects_external_or_invalid_urls(app, return_to):
    with app.test_client() as client:
        register(client, "安全返回用户")
        response = client.get("/help", query_string={"return_to": return_to})
        assert response.status_code == 200
        assert link_for_text(response, "返回上一页") == "/rooms"
        assert "example.com/steal" not in response.get_data(as_text=True)


def test_direct_help_uses_safe_fallback_and_valid_return_keeps_query_and_anchor(app):
    with app.test_client() as client:
        register(client, "直接帮助用户")
        direct = client.get("/help")
        assert link_for_text(direct, "返回上一页") == "/rooms"

        return_to = "/rooms?view=recent#current-room"
        refreshed = client.get("/help", query_string={"return_to": return_to})
        assert link_for_text(refreshed, "返回上一页") == return_to
        assert f'data-return-to="{return_to.replace("&", "&amp;")}"' in refreshed.get_data(as_text=True)


def test_help_navigation_selection_and_context_deep_links(app):
    with app.test_client() as owner:
        register(owner, "深链房主")
        room_id = create_room(owner)
        add_candidate(owner, room_id, "深链候选人")

        topic = owner.get(
            "/help/candidate-management",
            query_string={"return_to": f"/rooms/{room_id}/candidates"},
        )
        text = topic.get_data(as_text=True)
        assert topic.status_code == 200
        for title in (
            "快速开始",
            "账号与恢复码",
            "创建与加入房间",
            "候选人管理",
            "评分与保存",
            "成员与权限",
            "结果与排名",
            "房间设置",
            "常见问题",
        ):
            assert title in text
        assert re.search(
            r'class="help-nav-link is-active"[^>]+aria-current="page"[^>]*>.*候选人管理',
            text,
            re.DOTALL,
        )
        assert 'id="save-archive"' in text

        contextual_links = (
            (f"/rooms/{room_id}/candidates", "了解保存与归档", "/help/candidate-management", "save-archive"),
            (f"/rooms/{room_id}", "了解修改与撤销", "/help/scoring-saving", "edit-undo"),
            (f"/rooms/{room_id}/settings", "谁能看到结果？", "/help/results-ranking", "result-visibility"),
            (f"/rooms/{room_id}/settings", "了解房间状态", "/help/room-settings", "room-status"),
        )
        for source, label, expected_path, expected_anchor in contextual_links:
            link = link_for_text(owner.get(source), label)
            parsed = urlsplit(link)
            assert parsed.path == expected_path
            assert parsed.fragment == expected_anchor
            assert parse_qs(parsed.query)["return_to"] == [source]


def test_help_page_has_mobile_directory_history_behavior_and_accessible_entry(app):
    with app.test_client() as client:
        register(client, "帮助无障碍用户")
        response = client.get("/help/scoring-saving")
        text = response.get_data(as_text=True)
        assert '<details class="help-mobile-directory">' in text
        assert 'aria-label="帮助目录"' in text
        assert 'data-help-back' in text
        assert 'data-help-topic-link' in text
        assert 'aria-label="帮助"' in text

        script = client.get("/static/help.js").get_data(as_text=True)
        assert "window.history.replaceState" in script
        assert "window.history.back()" in script
        assert "window.location.replace" in script

        stylesheet = client.get("/static/style.css").get_data(as_text=True)
        assert "@media (max-width: 820px)" in stylesheet
        assert ".help-mobile-directory" in stylesheet
        assert "overflow-wrap: anywhere" in stylesheet
