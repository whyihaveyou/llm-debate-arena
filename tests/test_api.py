"""Tests for app.py API endpoints — auth, IDOR protection, file upload."""

import db


class TestAuth:
    def test_register(self, client):
        r = client.post("/api/auth/register", json={"username": "newuser", "password": "pass"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_register_duplicate(self, client):
        client.post("/api/auth/register", json={"username": "dup", "password": "pass"})
        r = client.post("/api/auth/register", json={"username": "dup", "password": "pass"})
        assert r.status_code == 409

    def test_register_short_username(self, client):
        r = client.post("/api/auth/register", json={"username": "", "password": "pass"})
        assert r.status_code == 400

    def test_register_short_password(self, client):
        r = client.post("/api/auth/register", json={"username": "okname", "password": ""})
        assert r.status_code == 400

    def test_login(self, client):
        client.post("/api/auth/register", json={"username": "loguser", "password": "password123"})
        r = client.post("/api/auth/login", json={"username": "loguser", "password": "password123"})
        assert r.status_code == 200
        assert "token" in r.json()

    def test_login_wrong_password(self, client):
        client.post("/api/auth/register", json={"username": "loguser2", "password": "password123"})
        r = client.post("/api/auth/login", json={"username": "loguser2", "password": "wrong"})
        assert r.status_code == 401

    def test_me_unauthenticated(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_me_authenticated(self, client, auth_headers):
        r = client.get("/api/auth/me", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["username"] == "testuser"

    def test_logout(self, client, auth_headers):
        r = client.post("/api/auth/logout", headers=auth_headers)
        assert r.status_code == 200


class TestIDORProtection:
    def test_list_debates_requires_auth(self, client):
        r = client.get("/api/debates")
        assert r.status_code == 401

    def test_list_debates_own_only(self, client, auth_headers):
        r = client.get("/api/debates", headers=auth_headers)
        assert r.status_code == 200
        # New user should have empty list
        data = r.json()
        assert "debates" in data
        assert data["debates"] == []

    def test_delete_nonexistent_session(self, client, auth_headers):
        r = client.delete("/api/debate/nonexistent-id", headers=auth_headers)
        assert r.status_code == 404

    def test_rename_nonexistent_session(self, client, auth_headers):
        r = client.put("/api/debate/nonexistent-id", json={"topic": "new"}, headers=auth_headers)
        assert r.status_code == 404

    def test_access_session_without_auth(self, client, auth_headers):
        # Try to access history without auth
        r = client.get("/api/debate/nonexistent/history")
        assert r.status_code in (401, 404)

    def test_message_too_long_rejected(self, client, auth_headers):
        # chat_send endpoint rejects messages > 32K chars
        huge_message = "x" * 40000
        r = client.post("/api/chat/send/nonexistent-id",
                        headers=auth_headers,
                        json={"message": huge_message})
        # Should get 400 (message too long) or 404 (session doesn't exist)
        assert r.status_code in (400, 404)
        if r.status_code == 400:
            assert "32768" in r.json()["detail"]


class TestFileUpload:
    def test_upload_requires_auth(self, client):
        r = client.post("/api/upload")
        assert r.status_code == 401

    def test_upload_large_file_rejected(self, client, auth_headers):
        # 11 MB should exceed 10 MB limit
        large_content = b"x" * (11 * 1024 * 1024)
        r = client.post(
            "/api/upload",
            headers=auth_headers,
            files={"file": ("big.txt", large_content, "text/plain")},
        )
        assert r.status_code == 413

    def test_upload_small_file_accepted(self, client, auth_headers):
        r = client.post(
            "/api/upload",
            headers=auth_headers,
            files={"file": ("small.txt", b"hello world", "text/plain")},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["filename"] == "small.txt"
        assert data["content"] == "hello world"


class TestModelEndpoints:
    def test_get_models(self, client, auth_headers):
        r = client.get("/api/user/models", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert isinstance(data["models"], list)

    def test_add_model(self, client, auth_headers):
        r = client.post("/api/user/models", headers=auth_headers, json={
            "name": "TestModel", "base_url": "https://api.example.com/v1",
            "api_key": "sk-test", "model": "test-1"
        })
        assert r.status_code == 200

    def test_add_model_requires_auth(self, client):
        r = client.post("/api/user/models", json={
            "name": "TestModel", "base_url": "https://api.example.com/v1",
            "api_key": "sk-test", "model": "test-1"
        })
        assert r.status_code == 401

    def test_delete_model(self, client, auth_headers):
        add_r = client.post("/api/user/models", headers=auth_headers, json={
            "name": "ToDelete", "base_url": "https://api.example.com/v1",
            "api_key": "sk-test", "model": "test-1"
        })
        model_id = add_r.json().get("id")
        if model_id:
            del_r = client.delete(f"/api/user/models/{model_id}", headers=auth_headers)
            assert del_r.status_code == 200

    def test_add_model_internal_ip_rejected(self, client, auth_headers):
        r = client.post("/api/user/models", headers=auth_headers, json={
            "name": "Evil", "base_url": "http://127.0.0.1:6379",
            "api_key": "sk-test", "model": "test-1"
        })
        assert r.status_code == 400

    def test_add_model_localhost_rejected(self, client, auth_headers):
        r = client.post("/api/user/models", headers=auth_headers, json={
            "name": "Evil", "base_url": "http://localhost:8080",
            "api_key": "sk-test", "model": "test-1"
        })
        assert r.status_code == 400

    def test_add_model_private_ip_rejected(self, client, auth_headers):
        for url in ("http://10.0.0.1/v1", "http://192.168.1.1/v1", "http://172.16.0.1/v1"):
            r = client.post("/api/user/models", headers=auth_headers, json={
                "name": "Evil", "base_url": url, "api_key": "sk-test", "model": "test-1"
            })
            assert r.status_code == 400, f"Expected 400 for {url}, got {r.status_code}"

    def test_add_model_non_http_rejected(self, client, auth_headers):
        r = client.post("/api/user/models", headers=auth_headers, json={
            "name": "Evil", "base_url": "ftp://api.example.com/v1",
            "api_key": "sk-test", "model": "test-1"
        })
        assert r.status_code == 400


class TestAdminEndpoints:
    def test_admin_stats_requires_admin(self, client, auth_headers):
        r = client.get("/api/admin/stats", headers=auth_headers)
        assert r.status_code == 403

    def test_admin_stats_as_admin(self, client, admin_headers):
        r = client.get("/api/admin/stats", headers=admin_headers)
        assert r.status_code == 200

    def test_admin_set_admin(self, client, admin_headers, auth_headers):
        me = client.get("/api/auth/me", headers=auth_headers).json()
        uid = me["id"]
        r = client.post("/api/admin/set-admin", headers=admin_headers,
                        json={"user_id": uid, "is_admin": True})
        assert r.status_code == 200
        me2 = client.get("/api/auth/me", headers=auth_headers).json()
        assert me2["is_admin"] is True
