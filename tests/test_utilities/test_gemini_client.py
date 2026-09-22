"""
Gemini client: key discovery, rotation, availability and quota classification.

The key-discovery tests exist because the pipeline must run on whatever one
account provides. A single key, under any of the accepted variable names, has to
behave exactly like a multi-key setup -- nothing may require two.
"""
import pytest

from src.config import config
from src.utilities import gemini_client
from src.utilities.gemini_client import (
    GeminiQuotaError,
    _get_api_keys,
    _next_key,
    describe_gemini_keys,
    gemini_generate_json,
    is_gemini_available,
    mark_gemini_exhausted,
    reset_gemini_exhausted,
)
from src.utilities.schema import ContactInfo


@pytest.fixture(autouse=True)
def _clean_gemini_state(monkeypatch):
    """Every test starts with no keys, no rotation state and no exhaustion flag."""
    reset_gemini_exhausted()
    monkeypatch.setattr(config, "gemini_api_keys", "")
    for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Pace the throttle out of the way; timing is not what these tests measure.
    monkeypatch.setattr(config, "gemini_rpm_per_key", 60000)
    monkeypatch.setattr(gemini_client, "_sdk_available", lambda: True)
    yield
    reset_gemini_exhausted()


# ---------------------------------------------------------------------------
# Key discovery
# ---------------------------------------------------------------------------

def test_is_gemini_available_with_keys(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "key-a,key-b")
    assert is_gemini_available() is True


def test_is_gemini_available_without_keys():
    assert _get_api_keys() == []
    assert is_gemini_available() is False


def test_a_single_key_is_a_complete_setup(monkeypatch):
    """One key is not a degraded mode: it is available and it is what gets used."""
    monkeypatch.setattr(config, "gemini_api_keys", "only-key")
    assert is_gemini_available() is True
    assert _get_api_keys() == ["only-key"]
    assert [_next_key() for _ in range(3)] == ["only-key"] * 3


@pytest.mark.parametrize("env_var", ["GEMINI_API_KEYS", "GEMINI_API_KEY", "GOOGLE_API_KEY"])
def test_any_accepted_env_var_supplies_the_key(monkeypatch, env_var):
    monkeypatch.setenv(env_var, "env-key")
    assert _get_api_keys() == ["env-key"]
    assert is_gemini_available() is True


def test_keys_split_on_commas_and_whitespace_and_dedupe(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", " k1, k2 k1,,k3 ")
    assert _get_api_keys() == ["k1", "k2", "k3"]


def test_config_keys_take_priority_over_environment(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "from-config")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert _get_api_keys() == ["from-config"]


def test_sdk_missing_makes_gemini_unavailable(monkeypatch):
    """`auto` must not select an engine whose client library is absent."""
    monkeypatch.setattr(config, "gemini_api_keys", "key-a")
    monkeypatch.setattr(gemini_client, "_sdk_available", lambda: False)
    assert is_gemini_available() is False


# ---------------------------------------------------------------------------
# Rotation and exhaustion
# ---------------------------------------------------------------------------

def test_round_robin_key_rotation(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1,k2,k3")
    assert [_next_key() for _ in range(7)] == ["k1", "k2", "k3", "k1", "k2", "k3", "k1"]


def test_rotation_restarts_when_the_configured_keys_change(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1,k2")
    assert _next_key() == "k1"
    monkeypatch.setattr(config, "gemini_api_keys", "z1,z2")
    # The stale cycle must not keep handing out keys that are no longer set.
    assert _next_key() in {"z1", "z2"}


def test_mark_gemini_exhausted(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1")
    assert is_gemini_available() is True
    mark_gemini_exhausted()
    assert is_gemini_available() is False
    reset_gemini_exhausted()
    assert is_gemini_available() is True


def test_next_key_without_configuration_raises():
    with pytest.raises(GeminiQuotaError, match="No Gemini API key configured"):
        _next_key()


def test_describe_gemini_keys_never_leaks_a_key(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "secret-one,secret-two")
    summary = describe_gemini_keys()
    assert "secret" not in summary
    assert "2 Gemini keys" in summary


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class _FakeModels:
    def __init__(self, behaviour):
        self._behaviour = behaviour
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self._behaviour(kwargs)


class _FakeClient:
    def __init__(self, behaviour, seen_keys, api_key=None, **_):
        seen_keys.append(api_key)
        self.aio = type("Aio", (), {"models": _FakeModels(behaviour)})()


def _install_fake_client(monkeypatch, behaviour):
    """Patch google.genai.Client; returns the list of keys it was constructed with."""
    from google import genai

    seen_keys = []
    monkeypatch.setattr(
        genai,
        "Client",
        lambda **kwargs: _FakeClient(behaviour, seen_keys, **kwargs),
    )
    return seen_keys


class _Response:
    def __init__(self, parsed=None, text=None):
        self.parsed = parsed
        self.text = text


class _HTTPError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@pytest.mark.asyncio
async def test_generate_json_returns_the_parsed_object(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1")
    expected = ContactInfo(official_email="admissions@example.edu")
    _install_fake_client(monkeypatch, lambda kwargs: _Response(parsed=expected))

    result = await gemini_generate_json("prompt", "system", ContactInfo)
    assert result is expected


@pytest.mark.asyncio
async def test_generate_json_falls_back_to_raw_text(monkeypatch):
    """An unparseable reply comes back as text rather than losing the request."""
    monkeypatch.setattr(config, "gemini_api_keys", "k1")
    _install_fake_client(monkeypatch, lambda kwargs: _Response(parsed=None, text='{"a": 1}'))

    assert await gemini_generate_json("prompt", "system", ContactInfo) == '{"a": 1}'


@pytest.mark.asyncio
async def test_gemini_quota_error_on_persistent_429(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1,k2")

    def _always_429(_kwargs):
        raise _HTTPError(429, "RESOURCE_EXHAUSTED: too many requests")

    seen = _install_fake_client(monkeypatch, _always_429)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    with pytest.raises(GeminiQuotaError, match="rate limited"):
        await gemini_generate_json("prompt", "system", ContactInfo, max_retries=3)

    # Each retry rotated onto the next key before giving up.
    assert seen == ["k1", "k2", "k1"]
    assert is_gemini_available() is False


@pytest.mark.asyncio
async def test_retry_succeeds_after_one_rate_limit(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1,k2")
    expected = ContactInfo(official_email="ok@example.edu")
    attempts = {"n": 0}

    def _429_then_ok(_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _HTTPError(429, "rate limit")
        return _Response(parsed=expected)

    _install_fake_client(monkeypatch, _429_then_ok)
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    assert await gemini_generate_json("prompt", "system", ContactInfo) is expected
    assert is_gemini_available() is True


@pytest.mark.asyncio
async def test_gemini_quota_error_on_403_is_immediate(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1")

    def _forbidden(_kwargs):
        raise _HTTPError(403, "PERMISSION_DENIED: quota exceeded")

    seen = _install_fake_client(monkeypatch, _forbidden)

    with pytest.raises(GeminiQuotaError, match="quota/permission"):
        await gemini_generate_json("prompt", "system", ContactInfo)

    assert len(seen) == 1  # no retries for an auth/quota rejection
    assert is_gemini_available() is False


@pytest.mark.asyncio
async def test_unclassified_errors_propagate_unchanged(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1")

    def _boom(_kwargs):
        raise ValueError("schema rejected")

    _install_fake_client(monkeypatch, _boom)

    with pytest.raises(ValueError, match="schema rejected"):
        await gemini_generate_json("prompt", "system", ContactInfo)
    # A bug in one request must not condemn the engine for the whole run.
    assert is_gemini_available() is True


@pytest.mark.asyncio
async def test_model_and_system_instruction_reach_the_api(monkeypatch):
    monkeypatch.setattr(config, "gemini_api_keys", "k1")
    monkeypatch.setattr(config, "gemini_model", "gemini-test-model")
    captured = {}

    def _capture(kwargs):
        captured.update(kwargs)
        return _Response(parsed=ContactInfo())

    _install_fake_client(monkeypatch, _capture)
    await gemini_generate_json("the prompt", "the system instruction", ContactInfo)

    assert captured["model"] == "gemini-test-model"
    assert captured["contents"] == "the prompt"
    assert captured["config"]["system_instruction"] == "the system instruction"
    assert captured["config"]["response_schema"] is ContactInfo
    assert captured["config"]["response_mime_type"] == "application/json"


async def _no_sleep(_seconds):
    return None
