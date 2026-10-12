"""Regression coverage for HAR JSON parsing boundaries."""

import importlib.util
from pathlib import Path


_SPEC = importlib.util.spec_from_file_location(
    "summarize_har", Path(__file__).parents[1] / "scripts" / "summarize_har.py"
)
assert _SPEC and _SPEC.loader
summarize_har = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(summarize_har)


def test_parse_json_preserves_unicode_and_rejects_empty_or_invalid_payloads():
    """HAR payloads may contain Unicode, while empty/malformed bodies are absent."""
    assert summarize_har.parse_json('{"message":"café \\u2603"}') == {
        "message": "café ☃"
    }
    for payload in (None, "", "   ", '{"message":'):
        assert summarize_har.parse_json(payload) is None
