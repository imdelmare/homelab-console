from app.services.redaction import REDACTED, is_sensitive_key, redact


def test_token_usage_counters_are_not_redacted():
    payload = {
        "input_tokens": 123,
        "output_tokens": 45,
        "total_tokens": 168,
        "cached_input_tokens": 80,
        "reasoning_tokens": 12,
        "max_output_tokens": 600,
    }

    assert redact(payload) == payload
    assert all(not is_sensitive_key(key) for key in payload)


def test_authentication_token_fields_remain_redacted():
    payload = {
        "token": "secret",
        "access_token": "secret",
        "refresh_token": "secret",
        "token_hint": "secret",
        "authorization": "Bearer secret",
    }

    assert redact(payload) == {key: REDACTED for key in payload}
    assert all(is_sensitive_key(key) for key in payload)
