import importlib
import py_compile
from pathlib import Path


def test_main_module_compiles_and_imports_app():
    main_path = Path(__file__).resolve().parents[1] / "main.py"

    py_compile.compile(str(main_path), doraise=True)
    module = importlib.import_module("main")

    assert hasattr(module, "app")
