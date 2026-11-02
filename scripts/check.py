"""No empty/missing safety groups or unexpected skips; no total-count threshold."""

from pathlib import Path
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import network_guard  # noqa: E402,F401 -- guard before test imports


def ids(group):
    for item in group:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    collected = set(ids(suite))
    for group in ["test_cache", "test_documents", "test_service", "test_sources", "test_transport", "test_release"]:
        if not any(name.startswith("tests." + group + ".") for name in collected):
            raise SystemExit("Missing safety group: " + group)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    allowed = (
        {"tests.test_transport.TransportTests.test_real_resource_limited_parser_child"}
        if os.name == "nt"
        else set()
    )
    if (
        not result.wasSuccessful()
        or not collected
        or result.testsRun != len(collected)
        or any(test.id() not in allowed for test, _ in result.skipped)
    ):
        raise SystemExit("Tests failed, missing or unexpectedly skipped")
