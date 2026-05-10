import os
import pytest
import tempfile
from pathlib import Path

# Point db module to a temp database
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

os.environ["DEBATE_ENCRYPTION_KEY"] = ""

# Patch DB_PATH before importing db
import db
db.DB_PATH = Path(_tmp_db.name)
db._shared_conn = None  # reset shared connection

from fastapi.testclient import TestClient
from app import app


@pytest.fixture(autouse=True)
def reset_db():
    """Wipe and reinitialize the database for each test."""
    if db._shared_conn is not None:
        try:
            db._shared_conn.close()
        except Exception:
            pass
        db._shared_conn = None
    Path(_tmp_db.name).unlink(missing_ok=True)
    db.init_db()
    yield
    if db._shared_conn is not None:
        try:
            db._shared_conn.close()
        except Exception:
            pass
        db._shared_conn = None


@pytest.fixture
def client(reset_db):
    """Create a TestClient with a fresh database."""
    c = TestClient(app)
    yield c


@pytest.fixture
def auth_headers(client):
    """Register a user and return auth headers."""
    client.post("/api/auth/register", json={"username": "testuser", "password": "testpass"})
    r = client.post("/api/auth/login", json={"username": "testuser", "password": "testpass"})
    data = r.json()
    assert "token" in data, f"Login failed: {data}"
    return {"Authorization": f"Bearer {data['token']}"}


@pytest.fixture
def admin_headers(client, auth_headers):
    """Create an admin user (separate from testuser)."""
    client.post("/api/auth/register", json={"username": "admin", "password": "adminpass"})
    r = client.post("/api/auth/login", json={"username": "admin", "password": "adminpass"})
    data = r.json()
    token = data["token"]
    # Make admin via direct db call
    user = db.verify_user("admin", "adminpass")
    if user:
        db.set_admin(user["id"], True)
    return {"Authorization": f"Bearer {token}"}
