import pytest
from app.models import User
from main import db

def test_healthz_endpoint(client):
    rv = client.get("/healthz")
    # 200 if enabled, or 501 (Not Implemented) if disabled
    assert rv.status_code in (200, 501)

def test_dashboard_requires_login(client, test_app):
    rv = client.get("/dashboard")
    # should redirect to login
    assert rv.status_code == 302
    assert "/login" in rv.headers["Location"]

def test_login_and_access_dashboard(client, test_app):
    # create a test user and set a password properly
    user = User(email="a@b.com")
    user.set_password("fakehash")
    db.session.add(user)
    db.session.commit()

    # simulate login (adjust field names as needed)
    login_data = {"email": "a@b.com", "password": "fakehash"}
    rv = client.post("/login", data=login_data, follow_redirects=True)
    assert b"Dashboard" in rv.data  # or another marker on your dashboard page
