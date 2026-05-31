import pytest
from fastapi import HTTPException

from core.config import settings
from core.security import require_owner_action


def test_owner_action_allows_dev_without_token():
    old_env = settings.APP_ENV
    old_token = settings.OWNER_API_TOKEN
    try:
        settings.APP_ENV = "development"
        settings.OWNER_API_TOKEN = ""
        assert require_owner_action(None) is True
    finally:
        settings.APP_ENV = old_env
        settings.OWNER_API_TOKEN = old_token


def test_owner_action_requires_matching_token_when_configured():
    old_env = settings.APP_ENV
    old_token = settings.OWNER_API_TOKEN
    try:
        settings.APP_ENV = "production"
        settings.OWNER_API_TOKEN = "secret-token"

        with pytest.raises(HTTPException) as exc:
            require_owner_action("wrong")

        assert exc.value.status_code == 401
        assert require_owner_action("secret-token") is True
    finally:
        settings.APP_ENV = old_env
        settings.OWNER_API_TOKEN = old_token
