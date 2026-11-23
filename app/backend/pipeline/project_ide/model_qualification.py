"""Check the exact local streaming tool transport against synthetic data."""
from __future__ import annotations

import json
import re
import secrets
from typing import Literal


def reviewer_tag(name: str) -> bool:
    # The reviewer exporter also creates scoped verification variants.
    return bool(re.fullmatch(r"aivf/reviewer-v40(?:-[^/:]+)?(?::[^/]+)?", name.lower()))


def qualification_key(model, server_version):
    return 1, server_version, model.get("endpoint_id"), model.get("tag"), model.get("digest"), model.get("context")


def narrated_tool_call(text: str, names: set[str]) -> bool:
    if not any(name in text for name in names):
        return False
    decoder = json.JSONDecoder()
    for index, match in enumerate(re.finditer(r"\{", text)):
        if index >= 64:
            break
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("name"), str) and value["name"] in names:
            if isinstance(value.get("arguments"), (dict, str)):
                return True
    return False


def native_model_settings(max_tokens, *, concise=False):
    settings = {"temperature": 0, "max_tokens": max_tokens}
    if concise:
        settings["extra_body"] = {"reasoning_effort": "none"}
    return settings


async def verify_local_tools(endpoint, key, name, *, context, concise=False):
    from httpx2 import AsyncClient
    from pydantic_ai import Agent, AgentRunResultEvent, UsageLimits
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.providers.ollama import OllamaProvider

    markers = {label: secrets.token_hex(5) for label in ("cedar", "maple")}
    called = []

    def read_probe_value(key: Literal["cedar", "maple"]) -> str:
        """Read the value for one key from an in-memory lookup table."""
        called.append(key)
        return markers[key]

    settings = native_model_settings(min(384, context // 4), concise=concise)
    async with AsyncClient(timeout=28, trust_env=False) as http:
        provider = OllamaProvider(base_url=endpoint, api_key=key, http_client=http)
        model = OllamaModel(name, provider=provider)
        agent = Agent(model, tools=[read_probe_value], retries=0, model_settings=settings)
        output = ""
        async with agent.run_stream_events(
            "Use read_probe_value to look up the key cedar. Then reply with its value alone.",
            usage_limits=UsageLimits(request_limit=2, tool_calls_limit=1), timeout=55,
        ) as events:
            async for event in events:
                if isinstance(event, AgentRunResultEvent):
                    output = event.result.output
    return called == ["cedar"] and markers["cedar"] in output and markers["maple"] not in output
