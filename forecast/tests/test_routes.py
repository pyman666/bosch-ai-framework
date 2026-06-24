"""路由层集成测试 — chat / skill / forecast 全部 API 端点。

使用临时 SQLite 数据库 + mock LLM 实现隔离测试。
"""

import os
import pytest

# 在 import 前设置环境变量，避免 settings.py 的 auth 检查失败
os.environ.setdefault("BAUTH_KEY", "test-key")
os.environ.setdefault("BAUTH_SECRET", "test-secret")


# ---------------------------------------------------------------------------
# 测试夹具 — 临时数据库 + TestClient
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    """使用临时 SQLite 数据库的测试客户端。"""
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from forecast.database import Base, get_db
    import forecast.db_models  # 确保 ORM 模型注册到 Base.metadata

    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    test_engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(test_engine, "connect")
    def _set_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)

    # 创建所有表
    Base.metadata.create_all(bind=test_engine)

    # Seed preset skills
    from forecast.core.skill_manager import seed_preset_skills
    seed_db = TestSessionLocal()
    try:
        seed_preset_skills(seed_db)
    finally:
        seed_db.close()

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    # 延迟 import app，此时 DB 已就绪
    from forecast.main import app
    app.dependency_overrides[get_db] = override_get_db

    # mock lifespan 以避免使用全局 DB
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def test_lifespan(app):
        yield

    old_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan

    from fastapi.testclient import TestClient
    import base64

    # 自动携带 Basic Auth 的默认 header（读取实际环境变量，避免与 setdefault 不一致）
    _user = os.environ.get("BAUTH_KEY", "test-key")
    _pass = os.environ.get("BAUTH_SECRET", "test-secret")
    auth_str = base64.b64encode(f"{_user}:{_pass}".encode()).decode()
    default_headers = {"Authorization": f"Basic {auth_str}"}

    try:
        with TestClient(app, headers=default_headers) as c:
            yield c
    finally:
        app.router.lifespan_context = old_lifespan
        app.dependency_overrides.clear()
        test_engine.dispose()


@pytest.fixture
def db_session(client, tmp_path):
    """直接访问数据库会话（用于设置测试数据）。"""
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    yield db
    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Chat Routes
# ---------------------------------------------------------------------------

class TestChatRoutes:
    """聊天会话路由测试。"""

    def test_create_session(self, client):
        resp = client.post("/api/v1/chat/sessions", json={"title": "Test Session"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["title"] == "Test Session"
        assert "id" in data
        assert data["messages"] == []

    def test_list_sessions(self, client):
        # 创建两个会话
        client.post("/api/v1/chat/sessions", json={"title": "Session A"})
        client.post("/api/v1/chat/sessions", json={"title": "Session B"})

        resp = client.get("/api/v1/chat/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 2
        # 最新在前
        assert sessions[0]["title"] == "Session B"

    def test_get_session(self, client):
        create_resp = client.post("/api/v1/chat/sessions", json={"title": "Get Test"})
        session_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/chat/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Get Test"

    def test_get_session_404(self, client):
        resp = client.get("/api/v1/chat/sessions/nonexistent-id")
        assert resp.status_code == 404

    def test_delete_session(self, client):
        create_resp = client.post("/api/v1/chat/sessions", json={"title": "Delete Me"})
        session_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/chat/sessions/{session_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # 确认已删除
        resp = client.get(f"/api/v1/chat/sessions/{session_id}")
        assert resp.status_code == 404

    def test_get_messages(self, client):
        create_resp = client.post("/api/v1/chat/sessions", json={"title": "Messages Test"})
        session_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/chat/sessions/{session_id}/messages")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_send_message_non_stream(self, client):
        """非流式发送消息（mock LLM）。"""
        from unittest.mock import patch, AsyncMock

        create_resp = client.post("/api/v1/chat/sessions", json={"title": "Send Test"})
        session_id = create_resp.json()["id"]

        async def mock_agent(**kwargs):
            return [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "I can help with forecast."},
            ]

        with patch("fcst.routes.chat.agent_non_streaming", new=mock_agent):
            resp = client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"message": "hello"},
                params={"stream": False},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "content" in data


# ---------------------------------------------------------------------------
# Skill Routes — CRUD
# ---------------------------------------------------------------------------

class TestSkillCRUD:
    """Skill CRUD 路由测试。"""

    def test_create_dsl_skill(self, client):
        resp = client.post("/api/v1/skills", json={
            "name": "Test DSL Skill",
            "description": "A test DSL skill",
            "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test DSL Skill"
        assert data["skill_type"] == "dsl"
        assert data["status"] == "draft"

    def test_create_preset_skill(self, client):
        resp = client.post("/api/v1/skills", json={
            "name": "Test Preset",
            "description": "Moving average preset",
            "skill_type": "preset",
            "preset_name": "moving_average",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["preset_name"] == "moving_average"

    def test_create_skill_missing_expression(self, client):
        """DSL skill 缺少 dsl_expression 应返回 400。"""
        resp = client.post("/api/v1/skills", json={
            "name": "Bad Skill",
            "description": "Missing expression",
            "skill_type": "dsl",
        })
        assert resp.status_code == 400

    def test_list_skills(self, client):
        client.post("/api/v1/skills", json={
            "name": "DSL 1", "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        client.post("/api/v1/skills", json={
            "name": "Preset 1", "description": "d", "skill_type": "preset",
            "preset_name": "moving_average",
        })

        resp = client.get("/api/v1/skills")
        assert resp.status_code == 200
        skills = resp.json()
        # 至少有刚创建的两个 + seed 的 presets
        assert len(skills) >= 2

    def test_list_skills_filter_type(self, client):
        client.post("/api/v1/skills", json={
            "name": "DSL Filter", "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })

        resp = client.get("/api/v1/skills", params={"skill_type": "dsl"})
        assert resp.status_code == 200
        dsl_skills = resp.json()
        assert all(s["skill_type"] == "dsl" for s in dsl_skills)

    def test_list_skills_filter_tag(self, client):
        """按 tag 过滤（preset skills 有 'preset' tag）。"""
        resp = client.get("/api/v1/skills", params={"tag": "preset"})
        assert resp.status_code == 200
        preset_skills = resp.json()
        assert len(preset_skills) > 0

    def test_list_presets(self, client):
        resp = client.get("/api/v1/skills/presets")
        assert resp.status_code == 200
        presets = resp.json()
        assert len(presets) >= 12  # 13 presets seeded
        assert all(s["skill_type"] == "preset" for s in presets)

    def test_get_skill(self, client):
        create_resp = client.post("/api/v1/skills", json={
            "name": "Get Skill", "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        skill_id = create_resp.json()["id"]

        resp = client.get(f"/api/v1/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Get Skill"

    def test_update_skill(self, client):
        create_resp = client.post("/api/v1/skills", json={
            "name": "Update Me", "description": "old", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        skill_id = create_resp.json()["id"]

        resp = client.put(f"/api/v1/skills/{skill_id}", json={
            "name": "Updated Name",
            "description": "new description",
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Name"
        assert resp.json()["description"] == "new description"

    def test_delete_skill(self, client):
        create_resp = client.post("/api/v1/skills", json={
            "name": "Delete Me", "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        skill_id = create_resp.json()["id"]

        resp = client.delete(f"/api/v1/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        resp = client.get(f"/api/v1/skills/{skill_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Skill Routes — Lifecycle
# ---------------------------------------------------------------------------

class TestSkillLifecycle:
    """Skill 生命周期测试（activate / review / deactivate / rollback）。"""

    def _create_dsl_skill(self, client, name="Lifecycle DSL"):
        resp = client.post("/api/v1/skills", json={
            "name": name, "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        return resp.json()["id"]

    def _create_python_skill(self, client, name="Lifecycle Python"):
        resp = client.post("/api/v1/skills", json={
            "name": name, "description": "d", "skill_type": "python",
            "python_code": "def forecast(record):\n    return [100]",
        })
        return resp.json()["id"]

    def test_activate_dsl_skill(self, client):
        skill_id = self._create_dsl_skill(client)
        resp = client.post(f"/api/v1/skills/{skill_id}/activate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_activate_python_without_review_fails(self, client):
        """Python skill 未 review 不能激活。"""
        skill_id = self._create_python_skill(client)
        resp = client.post(f"/api/v1/skills/{skill_id}/activate")
        assert resp.status_code == 400

    def test_review_python_skill(self, client):
        skill_id = self._create_python_skill(client)
        resp = client.post(f"/api/v1/skills/{skill_id}/review")
        assert resp.status_code == 200
        assert resp.json()["status"] == "reviewed"

    def test_review_then_activate_python(self, client):
        skill_id = self._create_python_skill(client)
        client.post(f"/api/v1/skills/{skill_id}/review")
        resp = client.post(f"/api/v1/skills/{skill_id}/activate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "active"

    def test_deactivate_skill(self, client):
        skill_id = self._create_dsl_skill(client)
        client.post(f"/api/v1/skills/{skill_id}/activate")
        resp = client.post(f"/api/v1/skills/{skill_id}/deactivate")
        assert resp.status_code == 200
        # deactivate 后状态变为 archived
        assert resp.json()["status"] == "archived"

    def test_rollback_skill(self, client):
        skill_id = self._create_dsl_skill(client)
        # 更新以产生版本
        client.put(f"/api/v1/skills/{skill_id}", json={"name": "Updated"})
        client.post(f"/api/v1/skills/{skill_id}/activate")

        resp = client.post(f"/api/v1/skills/{skill_id}/rollback", json={"version": 1})
        assert resp.status_code == 200
        assert resp.json()["status"] == "draft"

    def test_list_versions(self, client):
        skill_id = self._create_dsl_skill(client)
        client.put(f"/api/v1/skills/{skill_id}", json={"name": "V2"})

        resp = client.get(f"/api/v1/skills/{skill_id}/versions")
        assert resp.status_code == 200
        versions = resp.json()
        assert len(versions) >= 2

    def test_preview_skill(self, client):
        skill_id = self._create_dsl_skill(client, "Preview Test")
        resp = client.get(f"/api/v1/skills/{skill_id}/preview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_name"] == "Preview Test"
        assert "calculation_logic_md" in data


# ---------------------------------------------------------------------------
# Forecast Routes
# ---------------------------------------------------------------------------

class TestForecastRoutes:
    """预测执行路由测试。"""

    def _activate_preset(self, client, preset_name="moving_average"):
        """获取并激活一个 preset skill。"""
        resp = client.get("/api/v1/skills/presets")
        presets = resp.json()
        preset = next((p for p in presets if p["preset_name"] == preset_name), None)
        assert preset is not None
        client.post(f"/api/v1/skills/{preset['id']}/activate")
        return preset["id"]

    def _sample_input(self):
        return [{
            "carModel": "Test Car",
            "color": "Red",
            "demand": [
                {"date": "2026-01-01", "qty": 100},
                {"date": "2026-01-02", "qty": 120},
                {"date": "2026-01-03", "qty": 110},
            ],
            "pgi": [],
            "beginningInventory": 50,
        }]

    def test_run_forecast(self, client):
        skill_id = self._activate_preset(client)
        resp = client.post(
            f"/api/v1/forecast/run/{skill_id}",
            json=self._sample_input(),
        )
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) == 1
        assert "forecast" in results[0]

    def test_run_forecast_inactive_skill(self, client):
        """未激活的 skill 应返回 400。"""
        # 创建一个 DSL skill 但不激活
        create_resp = client.post("/api/v1/skills", json={
            "name": "Inactive", "description": "d", "skill_type": "dsl",
            "dsl_expression": "mean(demand)",
        })
        skill_id = create_resp.json()["id"]

        resp = client.post(
            f"/api/v1/forecast/run/{skill_id}",
            json=self._sample_input(),
        )
        assert resp.status_code == 400

    def test_run_forecast_not_found(self, client):
        resp = client.post(
            "/api/v1/forecast/run/nonexistent-id",
            json=self._sample_input(),
        )
        assert resp.status_code == 404

    def test_batch_forecast(self, client):
        skill_id = self._activate_preset(client)
        inputs = self._sample_input() + [{
            "carModel": "Car B", "color": "Blue",
            "demand": [{"date": "2026-01-01", "qty": 200}],
            "pgi": [], "beginningInventory": 100,
        }]

        resp = client.post(
            f"/api/v1/forecast/batch/{skill_id}",
            json={"skill_id": skill_id, "inputs": inputs},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_count"] == 2
        assert data["success_count"] == 2
        assert data["error_count"] == 0

    def test_batch_forecast_no_input(self, client):
        skill_id = self._activate_preset(client)
        resp = client.post(
            f"/api/v1/forecast/batch/{skill_id}",
            json={"skill_id": skill_id, "inputs": []},
        )
        assert resp.status_code == 400

    def test_trial_dsl(self, client):
        resp = client.post("/api/v1/forecast/trial", json={
            "dsl_expression": "mean(demand)",
            "input_data": self._sample_input(),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["error"] is None

    def test_trial_python(self, client):
        resp = client.post("/api/v1/forecast/trial", json={
            "python_code": "def forecast(record):\n    return [100, 200]",
            "input_data": self._sample_input(),
        })
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1

    def test_trial_missing_expression(self, client):
        resp = client.post("/api/v1/forecast/trial", json={
            "input_data": self._sample_input(),
        })
        assert resp.status_code == 400

    def test_evaluate_accuracy(self, client):
        resp = client.post("/api/v1/forecast/evaluate", json={
            "forecast": [
                {"date": "2026-01-01", "qty": 100},
                {"date": "2026-01-02", "qty": 120},
                {"date": "2026-01-03", "qty": 110},
            ],
            "actual": [
                {"date": "2026-01-01", "qty": 105},
                {"date": "2026-01-02", "qty": 115},
                {"date": "2026-01-03", "qty": 108},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        metrics = data["metrics"]
        assert "mae" in metrics
        assert "mape" in metrics
        assert "rmse" in metrics
        assert "smape" in metrics
        assert metrics["data_points"] == 3


# ---------------------------------------------------------------------------
# Health & Mock Endpoints
# ---------------------------------------------------------------------------

class TestSystemEndpoints:
    """系统端点测试。"""

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "fcst"

    def test_mock(self, client):
        resp = client.get("/mock")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
