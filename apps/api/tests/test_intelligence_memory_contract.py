import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_intelligence_memory_has_single_canonical_noisy_helpers():
    source = (ROOT / "apps/api/services/intelligence_memory.py").read_text()
    tree = ast.parse(source)

    module_level_helpers = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_is_noisy_decision", "_has_recent_same_noisy_event"}
    ]
    assert module_level_helpers == []

    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "IntelligenceMemory")
    class_methods = [node.name for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]

    assert class_methods.count("_is_noisy_decision") == 1
    assert class_methods.count("_has_recent_same_noisy_event") == 1
    assert source.count("return event") == 1
