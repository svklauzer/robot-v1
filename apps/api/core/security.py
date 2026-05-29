from datetime import datetime, timedelta, timezone
from fastapi import Header, HTTPException
from jose import jwt
from passlib.context import CryptContext
from core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)

def create_access_token(subject: str, expires_minutes: int = 60 * 24) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def require_owner_action(x_owner_token: str | None = Header(default=None, alias="X-Owner-Token")) -> bool:
    expected = settings.OWNER_API_TOKEN

    if settings.APP_ENV != "production" and not expected:
        return True

    if not expected:
        raise HTTPException(status_code=503, detail="owner_api_token_not_configured")

    if x_owner_token != expected:
        raise HTTPException(status_code=401, detail="owner_auth_required")

    return True
