"""
run_all.py — zero-dependency test runner (plain asserts, no pytest needed).

Usage: python tests/run_all.py
"""
import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scraper"))

MODULES = ["test_filters", "test_discovery", "test_pipeline"]


def main() -> int:
    passed, failed = 0, []
    for mod_name in MODULES:
        mod = importlib.import_module(mod_name)
        for name in sorted(dir(mod)):
            if not name.startswith("test_"):
                continue
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  PASS {mod_name}.{name}")
            except Exception:  # noqa: BLE001
                failed.append(f"{mod_name}.{name}")
                print(f"  FAIL {mod_name}.{name}")
                traceback.print_exc(limit=4)
    print(f"\n{passed} passed, {len(failed)} failed"
          + (f": {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
