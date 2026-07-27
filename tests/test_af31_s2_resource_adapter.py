"""AF31-S2 — Community resource/instruction adapter parity."""

from __future__ import annotations

import pytest


def test_af31_s2_community_instruction_provider_reads_prompt(tmp_path):
    from okto_pulse.community.adapters.resources import build_community_instruction_provider

    prompt = tmp_path / "agent_system_prompt.md"
    prompt.write_text("community prompt body", encoding="utf-8")

    provider = build_community_instruction_provider(prompt)

    assert provider.provider_id == "community"
    assert provider.load_instructions() == "community prompt body"


def test_af31_s2_community_registration_updates_core_mcp_instructions(tmp_path):
    from okto_pulse.community.adapters.resources import register_community_instruction_provider
    from okto_pulse.core.mcp import server

    prompt = tmp_path / "agent_system_prompt.md"
    prompt.write_text("community registered prompt", encoding="utf-8")

    server.reset_instruction_providers_for_tests()
    try:
        register_community_instruction_provider(prompt, freeze=True)

        assert server._load_instructions() == "community registered prompt"
        assert server.mcp.instructions == "community registered prompt"

        register_community_instruction_provider(prompt, freeze=False)
        assert server._load_instructions() == "community registered prompt"

        from okto_pulse.core.ports.mcp_instructions import StaticMcpInstructionProvider

        with pytest.raises(RuntimeError):
            server.register_instruction_provider(
                StaticMcpInstructionProvider(
                    provider_id="late-provider",
                    content="late prompt",
                )
            )
    finally:
        server.reset_instruction_providers_for_tests()
