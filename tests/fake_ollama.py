"""A small fake Ollama server for tests: a threaded http.server on 127.0.0.1 and a free port, answering from an in-memory model table and recording every request.

Use the ``fake_ollama`` fixture (import it into a test module); it points ``OLLAMA_HOST`` at the fake, so the client never reaches a real Ollama.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeOllama:
    """``models`` maps name -> size, ``running`` lists loaded names, ``requests`` holds ``(method, path, json_body)``."""

    def __init__(self) -> None:
        self.models: dict[str, int] = {"m:1": 1000}
        self.metadata: dict[str, dict] = {}
        self.running: list[str] = []
        self.contexts: dict[str, int] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.chat_stream = None
        self.qualification_stream = self.probe_reply
        self.fail_with: int | None = None     # when set, every route answers with this HTTP status
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def answer(self, method: str, path: str, body: dict | None) -> tuple[int, dict]:
        self.requests.append((method, path, body))
        if self.fail_with:
            return self.fail_with, {"error": "boom"}
        model = (body or {}).get("model")
        if isinstance(model, str) and ":" not in model and model + ":latest" in self.models:
            model += ":latest"
        if method == "GET" and path == "/api/version":
            return 200, {"version": "0.35.0"}
        if method == "GET" and path == "/api/tags":
            return 200, {"models": [{"name": n, "model": n, "size": s, "digest": "sha256:" + n.replace(":", ""),
                                     **self.metadata.get(n, {}).get("tag", {})} for n, s in self.models.items()]}
        if method == "POST" and path == "/api/show":
            if model not in self.models:
                return 404, {"error": f"model '{model}' not found"}
            return 200, self.metadata.get(model, {}).get("show", {})
        if method == "POST" and path == "/api/create":
            source = (body or {}).get("from")
            if source not in self.models:
                return 404, {"error": "source model not found"}
            model = model if ":" in model else model + ":latest"
            self.models[model] = self.models[source]
            self.metadata[model] = {"show": {**self.metadata[source]["show"],
                                            "parameters": "num_ctx " + str(body["parameters"]["num_ctx"])}}
            self.contexts[model] = body["parameters"]["num_ctx"]
            return 200, {"status": "success"}
        if method == "GET" and path == "/api/ps":
            return 200, {"models": [{"name": n, "model": n, "size": self.models.get(n, 0), "size_vram": self.models.get(n, 0),
                                     "context_length": self.contexts.get(n, 0), "expires_at": "2099-01-01T00:00:00Z"} for n in self.running]}
        if method == "POST" and path in ("/api/generate", "/api/chat", "/api/embed", "/v1/chat/completions", "/v1/embeddings"):
            if model not in self.models and model not in self.running and model is not None and ":" not in model:
                model += ":latest"
            if model not in self.models and model not in self.running:   # a model set running by a test is known too
                return 404, {"error": f"model '{model}' not found"}
            return self._post(path, {**(body or {}), "model": model})
        return 404, {"error": "unknown route"}

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        model = body["model"]
        if path == "/api/generate":
            if body.get("keep_alive") in (0, "0"):
                self.running = [n for n in self.running if n != model]
            elif model not in self.running:
                self.running.append(model)
            if context := body.get("options", {}).get("num_ctx"):
                self.contexts[model] = context
            return 200, {"model": model, "response": "", "done": True}
        if path == "/api/embed":
            if model not in self.running:
                self.running.append(model)
            return 200, {"model": model, "embeddings": [[float(len(t)), 1.0] for t in body.get("input", [])]}
        if path == "/api/chat":
            return 200, {"model": model, "message": {"role": "assistant", "content": "Reviewed."}, "done": True}
        if path == "/v1/embeddings":
            return 200, {"object": "list", "model": model, "data": [{"object": "embedding", "index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(body.get("input", []))]}
        return 200, {"object": "chat.completion", "model": model, "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}

    @staticmethod
    def probe_reply(body):
        received = next((message for message in body["messages"] if message["role"] == "tool"), None)
        if not received:
            yield {"tool_calls": [{"index": 0, "id": "qualification-fixture", "type": "function",
                                   "function": {"name": "read_probe_value", "arguments": '{"key":"cedar"}'}}]}
        else:
            yield {"content": received["content"]}


def _make_handler(fake: FakeOllama):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, method: str) -> None:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(size) or b"null") if size else None
            status, payload = fake.answer(method, self.path, body)
            data = json.dumps(payload).encode("utf-8")
            try:
                if status == 200 and self.path == "/v1/chat/completions" and isinstance(body, dict) and body.get("stream"):
                    qualification = any(tool["function"]["name"] == "read_probe_value" for tool in body.get("tools", ()))
                    stream = fake.qualification_stream if qualification else fake.chat_stream
                    if stream:
                        self._stream_chat(body, stream)
                        return
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except ConnectionError:
                pass  # A timeout test may close its connection before the fake answers.

        def _stream_chat(self, body, stream):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def chunk(delta, finish_reason=None):
                data = {"id": "fixture", "created": 1, "model": body["model"], "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                self.wfile.flush()

            chunk({"role": "assistant"})
            finish = "stop"
            for delta in stream(body):
                chunk(delta)
                if "tool_calls" in delta:
                    finish = "tool_calls"
            chunk({}, finish)
            self.wfile.write(b"data: [DONE]\n\n")

        def do_GET(self) -> None:
            self._reply("GET")

        def do_POST(self) -> None:
            self._reply("POST")

        def log_message(self, *args) -> None:
            pass

    return Handler


@pytest.fixture
def fake_ollama(monkeypatch):
    fake = FakeOllama()
    fake.start()
    monkeypatch.setenv("OLLAMA_HOST", fake.host)
    try:
        yield fake
    finally:
        fake.stop()
