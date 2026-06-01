from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_funding_arb_owner_page_wires_api_contracts():
    page = (ROOT / "apps/web/app/funding/page.tsx").read_text()

    assert "/funding-arb/summary" in page
    assert "/funding-arb/opportunities?limit=50" in page
    assert "/funding-arb/positions?limit=50" in page
    assert "/funding-arb/scan" in page
    assert "/funding-arb/open" in page
    assert "/funding-arb/evaluate-exits" in page
    assert "mode: \"paper\"" in page


def test_owner_nav_and_health_surface_funding_arbitrage():
    nav = (ROOT / "apps/web/components/Nav.tsx").read_text()
    health = (ROOT / "apps/web/app/health/page.tsx").read_text()

    assert "href: \"/funding\"" in nav
    assert "Funding Arb" in nav
    assert "Funding Arb Loop" in health
    assert "HTX funding arb" in health
    assert "funding_arb_loop" in health


def test_payments_owner_page_exposes_revenue_dashboard_contract():
    page = (ROOT / "apps/web/app/payments/page.tsx").read_text()

    assert "/payments/revenue?window_days=30" in page
    assert "MRR est." in page
    assert "30d cash" in page
    assert "Revenue funnel" in page
    assert "Trial→Paid" in page


def test_owner_health_and_analytics_surface_vip_delivery_sla():
    health = (ROOT / "apps/web/app/health/page.tsx").read_text()
    analytics = (ROOT / "apps/web/app/analytics/page.tsx").read_text()

    assert "Telegram delivery 24h" in health
    assert "VIP SLA" in health
    assert "vip_sla_pct" in health
    assert "VIP queued" in health
    assert "vip_queued" in health
    assert "VIP SLA" in analytics
    assert "vip_sla_pct" in analytics
    assert "VIP queued" in analytics


def test_analytics_page_surfaces_symbol_profitability_guard():
    page = (ROOT / "apps/web/app/analytics/page.tsx").read_text()

    assert "/analytics/symbol-performance?lookback=12" in page
    assert "Per-symbol profitability guard" in page
    assert "Risk x" in page
    assert "Failed setup" in page
    assert "symbolPerf?.blocked_count" in page
    assert "symbolPerf?.reduced_count" in page
