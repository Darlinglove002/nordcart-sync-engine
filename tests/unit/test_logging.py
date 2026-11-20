from __future__ import annotations

import logging

from app.logging_config import FORMAT, configure_logging


def test_log_level_comes_from_the_environment(monkeypatch):
    root = logging.getLogger()
    before = root.level
    try:
        monkeypatch.setenv("LOG_LEVEL", "debug")
        configure_logging()
        assert root.level == logging.DEBUG
        monkeypatch.setenv("LOG_LEVEL", "nonsense")
        configure_logging()
        assert root.level == logging.INFO  # a typo must not stop the service from starting
    finally:
        root.setLevel(before)


def test_configure_logging_installs_one_handler_and_is_idempotent():
    root = logging.getLogger()
    saved, level = root.handlers[:], root.level
    root.handlers = []
    try:
        configure_logging("INFO")
        configure_logging("INFO")
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter
        assert formatter is not None and formatter._fmt == FORMAT
    finally:
        root.handlers = saved
        root.setLevel(level)
