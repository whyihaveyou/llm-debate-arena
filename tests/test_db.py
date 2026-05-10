"""Tests for db.py — user management, sessions, models, quota, SQL injection."""

import db


class TestUserManagement:
    def test_create_user(self):
        assert db.create_user("alice", "pass1234") is True
        assert db.create_user("alice", "other") is False  # duplicate

    def test_verify_user(self):
        db.create_user("bob", "secret")
        assert db.verify_user("bob", "secret") is not None
        assert db.verify_user("bob", "wrong") is None
        assert db.verify_user("nobody", "secret") is None

    def test_sql_injection_username(self):
        # Should not crash; username is treated as a string, not SQL
        db.create_user("'; DROP TABLE users; --", "pass")
        # Table should still exist
        assert db.verify_user("'; DROP TABLE users; --", "pass") is not None


class TestSessionManagement:
    def test_create_and_verify_session(self):
        db.create_user("sess_user", "pw")
        user = db.verify_user("sess_user", "pw")
        token = db.create_session(user["id"])
        result = db.verify_session(token)
        assert result is not None
        assert result["user_id"] == user["id"]
        assert result["username"] == "sess_user"

    def test_invalid_session(self):
        assert db.verify_session("") is None
        assert db.verify_session("short") is None
        assert db.verify_session("x" * 64) is None

    def test_delete_session(self):
        db.create_user("del_user", "pw")
        user = db.verify_user("del_user", "pw")
        token = db.create_session(user["id"])
        db.delete_session(token)
        assert db.verify_session(token) is None


class TestModelCRUD:
    def _make_user(self):
        db.create_user("model_user", "pw")
        return db.verify_user("model_user", "pw")["id"]

    def test_add_and_get_model(self):
        uid = self._make_user()
        mid = db.add_user_model(uid, "MyModel", "https://api.example.com/v1", "sk-test", "model-1")
        assert mid > 0
        models = db.get_user_models(uid)
        assert len(models) == 1
        assert models[0]["name"] == "MyModel"

    def test_get_model_with_decrypted_key(self):
        uid = self._make_user()
        mid = db.add_user_model(uid, "M", "https://api.example.com/v1", "secret-key", "m")
        m = db.get_user_model(uid, mid)
        assert m["api_key"] == "secret-key"

    def test_update_model(self):
        uid = self._make_user()
        mid = db.add_user_model(uid, "Old", "https://old.com/v1", "key", "old")
        assert db.update_user_model(mid, uid, name="New")
        models = db.get_user_models(uid)
        assert models[0]["name"] == "New"

    def test_update_wrong_user(self):
        uid = self._make_user()
        db.create_user("other", "pw")
        oid = db.verify_user("other", "pw")["id"]
        mid = db.add_user_model(uid, "M", "https://a.com/v1", "k", "m")
        assert db.update_user_model(mid, oid, name="Hacked") is False

    def test_delete_model(self):
        uid = self._make_user()
        mid = db.add_user_model(uid, "M", "https://a.com/v1", "k", "m")
        db.delete_user_model(mid, uid)
        assert db.get_user_models(uid) == []

    def test_get_model_nonexistent(self):
        assert db.get_user_model(99999, 99999) is None


class TestQuota:
    def test_initial_quota(self):
        db.create_user("quota_user", "pw")
        uid = db.verify_user("quota_user", "pw")["id"]
        assert db.get_quota_usage(uid) == 0

    def test_increment_quota(self):
        db.create_user("quota_user2", "pw")
        uid = db.verify_user("quota_user2", "pw")["id"]
        db.increment_quota_usage(uid)
        assert db.get_quota_usage(uid) == 1
        db.increment_quota_usage(uid)
        assert db.get_quota_usage(uid) == 2


class TestAdmin:
    def test_set_admin(self):
        db.create_user("adm_user", "pw")
        uid = db.verify_user("adm_user", "pw")["id"]
        assert db.get_user_by_id(uid)["is_admin"] == 0
        db.set_admin(uid, True)
        assert db.get_user_by_id(uid)["is_admin"] == 1

    def test_get_all_users(self):
        db.create_user("u1", "p1")
        db.create_user("u2", "p2")
        users = db.get_all_users()
        assert len(users) >= 2
        names = {u["username"] for u in users}
        assert "u1" in names
        assert "u2" in names

    def test_get_all_usage_stats(self):
        stats = db.get_all_usage_stats()
        assert isinstance(stats, list)
