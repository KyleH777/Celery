"""Authentication: registration, login, token verification, hashing."""

import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.auth import hash_password, verify_password
from app.config import settings
from tests.conftest import register_and_login


class TestRegistration:
    def test_register_returns_user_without_password_material(self, client):
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "alice@example.com", "password": "s3cretpass"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["email"] == "alice@example.com"
        assert "password" not in body
        assert "hashed_password" not in body

    def test_duplicate_email_conflicts_case_insensitively(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "Alice@Example.com", "password": "s3cretpass"},
        )
        response = client.post(
            "/api/v1/auth/register",
            json={"email": "alice@example.com", "password": "otherpass99"},
        )
        assert response.status_code == 409

    def test_short_password_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register", json={"email": "bob@example.com", "password": "short"}
        )
        assert response.status_code == 422

    def test_invalid_email_rejected(self, client):
        response = client.post(
            "/api/v1/auth/register", json={"email": "not-an-email", "password": "password123"}
        )
        assert response.status_code == 422


class TestLogin:
    def test_valid_credentials_return_bearer_token(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "alice@example.com", "password": "s3cretpass"},
        )
        response = client.post(
            "/api/v1/auth/token", data={"username": "alice@example.com", "password": "s3cretpass"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]

    def test_wrong_password_rejected(self, client):
        client.post(
            "/api/v1/auth/register",
            json={"email": "alice@example.com", "password": "s3cretpass"},
        )
        response = client.post(
            "/api/v1/auth/token", data={"username": "alice@example.com", "password": "wrong"}
        )
        assert response.status_code == 401

    def test_unknown_user_gets_same_generic_401(self, client):
        response = client.post(
            "/api/v1/auth/token", data={"username": "ghost@example.com", "password": "whatever"}
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect email or password."


class TestTokenVerification:
    def test_missing_token_rejected(self, client):
        assert client.get(f"/api/v1/pitches/{uuid.uuid4()}").status_code == 401
        assert (
            client.post("/api/v1/pitches/enrich", json={"domain": "acme.com"}).status_code == 401
        )

    def test_forged_signature_rejected(self, client):
        forged = jwt.encode(
            {"sub": str(uuid.uuid4()), "type": "access"}, "wrong-secret", algorithm="HS256"
        )
        response = client.get(
            f"/api/v1/pitches/{uuid.uuid4()}", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    def test_expired_token_rejected(self, client):
        register_and_login(client)
        expired = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "access",
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        response = client.get(
            f"/api/v1/pitches/{uuid.uuid4()}", headers={"Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401
        assert "expired" in response.json()["detail"].lower()

    def test_token_without_expiry_rejected(self, client):
        no_exp = jwt.encode(
            {"sub": str(uuid.uuid4()), "type": "access"},
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        response = client.get(
            f"/api/v1/pitches/{uuid.uuid4()}", headers={"Authorization": f"Bearer {no_exp}"}
        )
        assert response.status_code == 401

    def test_garbage_token_rejected(self, client):
        response = client.get(
            f"/api/v1/pitches/{uuid.uuid4()}", headers={"Authorization": "Bearer not.a.jwt"}
        )
        assert response.status_code == 401


class TestPasswordHashing:
    def test_bcrypt_hash_is_salted_and_verifiable(self):
        hashed = hash_password("hunter2hunter2")
        assert hashed.startswith("$2b$")
        assert "hunter2" not in hashed
        assert verify_password("hunter2hunter2", hashed)
        assert not verify_password("wrong-password", hashed)
        assert hash_password("hunter2hunter2") != hashed

    def test_malformed_hash_fails_closed(self):
        assert verify_password("anything", "not-a-real-hash") is False
