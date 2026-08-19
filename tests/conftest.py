import logging

import pytest


@pytest.fixture(autouse=True)
def _reset_aiform_logger():
    yield
    logger = logging.getLogger("aiform")
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
