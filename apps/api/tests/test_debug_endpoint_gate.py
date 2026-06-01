import pytest
from fastapi import HTTPException

from core.config import settings
from core.security import require_non_production_debug


def test_debug_gate_blocks_production():
    old_env = settings.APP_ENV
    try:
        settings.APP_ENV = "production"
        with pytest.raises(HTTPException) as exc:
            require_non_production_debug()

        assert exc.value.status_code == 403
        assert exc.value.detail == "debug_endpoints_disabled_in_production"
    finally:
        settings.APP_ENV = old_env


def test_debug_gate_allows_development():
    old_env = settings.APP_ENV
    try:
        settings.APP_ENV = "development"
        assert require_non_production_debug() is True
    finally:
        settings.APP_ENV = old_env
