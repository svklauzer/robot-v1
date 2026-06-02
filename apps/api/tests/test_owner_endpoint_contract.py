from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_sensitive_owner_endpoints_require_owner_auth():
    main = (ROOT / "apps/api/main.py").read_text()

    sensitive_routes = [
        ('post', '/bot/start'),
        ('post', '/bot/stop'),
        ('post', '/robot/run-once'),
        ('post', '/signals/{signal_id}/close'),
        ('post', '/signals/maintenance/queued-to-published'),
        ('post', '/reports/send-owner'),
        ('post', '/reports/send-free'),
        ('post', '/reports/send-vip'),
        ('post', '/reports/send-all'),
        ('post', '/subscribers'),
        ('post', '/payments/checkout'),
        ('post', '/payments/events'),
        ('post', '/payments/reconcile'),
        ('post', '/funding-arb/scan'),
        ('post', '/funding-arb/open'),
        ('post', '/funding-arb/paper-smoke'),
        ('post', '/system/kill-switch'),
        ('post', '/system/kill-switch-smoke'),
        ('post', '/trade/cost-preview'),
        ('post', '/trade/build-plan'),
        ('post', '/intelligence/scan/run'),
    ]

    for method, route in sensitive_routes:
        prefix = f'@app.{method}("{route}"'
        line = next(line for line in main.splitlines() if line.startswith(prefix))
        assert 'Depends(require_owner_action)' in line, route


def test_owner_read_endpoints_require_owner_auth():
    main = (ROOT / "apps/api/main.py").read_text()

    owner_read_routes = [
        "/subscribers",
        "/system/health",
        "/system/exchange-reconciliation",
        "/system/live-safety",
        "/system/readiness",
        "/audit/events",
        "/payments",
        "/payments/events",
        "/payments/summary",
        "/payments/revenue",
        "/funding-arb/summary",
        "/funding-arb/opportunities",
        "/funding-arb/positions",
        "/telegram/deliveries/summary",
    ]
    for route in owner_read_routes:
        line = next(line for line in main.splitlines() if line.startswith(f'@app.get("{route}"'))
        assert 'Depends(require_owner_action)' in line, route


def test_public_telegram_webhook_stays_public_for_telegram_callbacks():
    main = (ROOT / "apps/api/main.py").read_text()

    line = next(line for line in main.splitlines() if '"/telegram/webhook"' in line and line.startswith('@app.'))
    assert 'Depends(require_owner_action)' not in line
