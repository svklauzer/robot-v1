from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_funding_arb_owner_page_wires_api_contracts():
    page = (ROOT / "apps/web/app/funding/page.tsx").read_text()

    assert "/funding-arb/summary" in page
    assert "/funding-arb/opportunities?limit=50" in page
    assert "/funding-arb/positions?limit=50" in page
    assert "/funding-arb/scan" in page
    assert "/funding-arb/open" in page
    assert "mode: \"paper\"" in page


def test_owner_nav_and_health_surface_funding_arbitrage():
    nav = (ROOT / "apps/web/components/Nav.tsx").read_text()
    health = (ROOT / "apps/web/app/health/page.tsx").read_text()

    assert "href: \"/funding\"" in nav
    assert "Funding Arb" in nav
    assert "Funding Arb Loop" in health
    assert "HTX funding arb" in health
    assert "funding_arb_loop" in health
