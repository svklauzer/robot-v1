from pathlib import Path

import yaml


def _robot_api_env() -> dict[str, str]:
    blueprint_path = Path(__file__).resolve().parents[3] / "render.yaml"
    blueprint = yaml.safe_load(blueprint_path.read_text(encoding="utf-8"))

    for service in blueprint["services"]:
        if service.get("type") == "web" and service.get("name") == "robot-api":
            return {
                item["key"]: str(item["value"])
                for item in service.get("envVars", [])
                if "value" in item
            }

    raise AssertionError("robot-api service is missing from render.yaml")


def test_render_blueprint_keeps_capital_leak_guards_enabled():
    """Render Blueprint env must match protective trading defaults in config.py."""
    env = _robot_api_env()

    assert env["REGIME_EXP_SIZING_ENABLED"].lower() == "true"
    assert env["ENABLE_GRADE_B_TRADING"].lower() == "false"
