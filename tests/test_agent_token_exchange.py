"""
Unit tests for the Registry Governor token-exchange guard (agent/agent.py).

Verifies the fail-closed behaviour: with no DaVinci exchange configured the
agent must refuse to forward the (wrong-audience) agent_token to the gateway,
unless the explicit dev-only ALLOW_TOKEN_PASSTHROUGH escape hatch is set.

Skipped automatically if google-adk (an import-time dependency of agent.py)
is not installed.
"""
import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_agent():
    """Import agent/agent.py with the real `mcp` SDK visible.

    The repo has a top-level `mcp/` directory (the SpiceDB bridge), which
    shadows the installed `mcp` package that google-adk imports. With the repo
    root on sys.path the shadow wins, google.adk raises ImportError, and these
    tests skip silently — passing without ever running. Drop the repo root for
    the duration of the import so the installed SDK resolves.
    """
    saved = list(sys.path)
    sys.path[:] = [p for p in sys.path if p not in ("", ".", str(_ROOT))]
    try:
        pytest.importorskip("google.adk", reason="google-adk not installed")
        spec = importlib.util.spec_from_file_location(
            "registry_agent_mod", _ROOT / "agent" / "agent.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path[:] = saved
    return mod


def test_fail_closed_without_davinci_key():
    agent = _load_agent()
    agent._DAVINCI_POLICY_API_KEY = ""
    agent._ALLOW_TOKEN_PASSTHROUGH = False
    with pytest.raises(RuntimeError):
        agent._exchange_for_mcp_token("some.agent.token")


def test_passthrough_only_when_explicitly_enabled():
    agent = _load_agent()
    agent._DAVINCI_POLICY_API_KEY = ""
    agent._ALLOW_TOKEN_PASSTHROUGH = True
    assert agent._exchange_for_mcp_token("raw-token") == "raw-token"


class _FakeCallbackContext:
    """Minimal stand-in for ADK's CallbackContext.

    Only `state` is needed: the unauthenticated paths return before touching
    `_invocation_context`, which is exactly the behaviour under test.
    """

    def __init__(self, headers: dict):
        self.state = {"headers": headers}


def test_missing_auth_short_circuits_before_the_model():
    """No credential must end the turn, not run the model without tools.

    Returning None here would let the LlmAgent execute and bill a model call —
    the defect that made this endpoint usable as a free LLM.
    """
    agent = _load_agent()
    result = agent.inject_mcp_auth(_FakeCallbackContext({}))
    assert result is not None, "unauthenticated turn must not reach the model"
    assert result.parts[0].text  # a refusal is surfaced to the caller


def test_failed_exchange_short_circuits_before_the_model(monkeypatch):
    """A token that fails verification must also end the turn.

    An expired, forged, or wrong-audience token raises during the exchange;
    that exception must not be swallowed into a tool-less model call.
    """
    agent = _load_agent()

    def _boom(_token):
        raise RuntimeError("aud validation failed")

    monkeypatch.setattr(agent, "_exchange_for_mcp_token", _boom)
    result = agent.inject_mcp_auth(
        _FakeCallbackContext({"agent_authorization": "Bearer forged.token.here"})
    )
    assert result is not None, "unverifiable token must not reach the model"
