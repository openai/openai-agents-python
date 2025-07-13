import os
import sys


# Skip voice tests on Python 3.9
def pytest_ignore_collect(collection_path, config):
    if sys.version_info[:2] == (3, 9):
        this_dir = os.path.dirname(__file__)

        if str(collection_path).startswith(this_dir):
            return True
