import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_aiform_logger():
    yield
    logger = logging.getLogger("aiform")
    # close(), not just clear() -- a FileHandler holds a real OS file
    # descriptor open until closed; leaving hundreds of them open across
    # a full test run (one FileHandler per configure() call) risks
    # exhausting the process's fd limit and can block tmp_path cleanup
    # on platforms that refuse to delete a file still held open.
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
