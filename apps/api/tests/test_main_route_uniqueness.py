import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ROUTE_RE = re.compile(r'^@app\.(get|post|put|delete|patch)\("([^"]+)"')


def test_main_has_no_duplicate_route_decorators():
    """Prevent unreachable FastAPI handlers after large manual merges."""
    main = (ROOT / "apps/api/main.py").read_text().splitlines()
    routes = defaultdict(list)

    for line_no, line in enumerate(main, start=1):
        match = ROUTE_RE.match(line)
        if match:
            method, path = match.groups()
            routes[(method.upper(), path)].append(line_no)

    duplicates = {
        f"{method} {path}": line_numbers
        for (method, path), line_numbers in routes.items()
        if len(line_numbers) > 1
    }

    assert duplicates == {}
