"""forge-unattended-publish-v1 P3 (mechanism 2) — notify.py:91 severity-conditional
log prefix. An info notice gets a clean line; error/warning KEEP [ACTION FAILURE]
byte-for-byte, so every existing failure caller (all at default error severity) is
unchanged.
"""

import asyncio
import logging

import grove.notify as notify


def _log_line(caplog, severity: str) -> str:
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=notify.logger.name):
        # No adapters configured in the test env → only the log leg fires.
        asyncio.run(notify.broadcast_to_operator("the message", severity=severity))
    # The content log line is the one carrying "the message".
    hits = [r.getMessage() for r in caplog.records if "the message" in r.getMessage()]
    assert hits, f"no content log line for severity={severity!r}"
    return hits[0]


def test_info_caller_gets_clean_prefix(caplog):
    line = _log_line(caplog, "info")
    assert line == "the message"
    assert "[ACTION FAILURE]" not in line


def test_error_caller_keeps_action_failure_prefix(caplog):
    # The default severity for every existing failure caller.
    assert _log_line(caplog, "error") == "[ACTION FAILURE] the message"


def test_warning_caller_keeps_action_failure_prefix(caplog):
    assert _log_line(caplog, "warning") == "[ACTION FAILURE] the message"


def test_unknown_severity_keeps_failure_prefix(caplog):
    # getattr fallback logs at error; the prefix stays failure-flavored.
    assert _log_line(caplog, "bogus") == "[ACTION FAILURE] the message"
