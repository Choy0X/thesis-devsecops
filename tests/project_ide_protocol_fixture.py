def completion():
    import json
    return {"id": "fixture", "model": "llm", "choices": [{"index": 0, "finish_reason": "stop", "message": {
        "role": "assistant", "content": json.dumps({"kind": "call", "name": "read", "arguments": {"path": "main.py"}})}}]}
