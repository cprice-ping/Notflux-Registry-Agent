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

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_agent():
    pytest.importorskip("google.adk", reason="google-adk not installed")
    spec = importlib.util.spec_from_file_location("registry_agent_mod", _ROOT / "agent" / "agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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
