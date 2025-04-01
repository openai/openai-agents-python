import os
import sys

import pytest


def pytest_collection_modifyitems(config, items):
    if sys.version_info[:2] == (3, 9):
        this_dir = os.path.dirname(__file__)
        skip_marker = pytest.mark.skip(reason="Skipped on Python 3.9")

        for item in items:
            if item.fspath.dirname.startswith(this_dir):
                item.add_marker(skip_marker)
