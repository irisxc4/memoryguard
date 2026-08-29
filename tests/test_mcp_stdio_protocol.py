from __future__ import annotations

from memoryguard.mcp_server import handle_request


def test_initialize_advertises_tools_resources_and_prompts() -> None:
    response = handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )

    assert response is not None
    capabilities = response["result"]["capabilities"]
    assert capabilities == {"tools": {}, "resources": {}, "prompts": {}}


def test_empty_resource_surfaces_are_protocol_compliant() -> None:
    resources = handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": {}}
    )
    templates = handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/templates/list",
            "params": {},
        }
    )

    assert resources == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"resources": []},
    }
    assert templates == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"resourceTemplates": []},
    }


def test_empty_prompt_surface_is_protocol_compliant() -> None:
    prompts = handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}}
    )

    assert prompts == {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"prompts": []},
    }


def test_ping_returns_empty_success_result() -> None:
    assert handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}}
    ) == {"jsonrpc": "2.0", "id": 5, "result": {}}
