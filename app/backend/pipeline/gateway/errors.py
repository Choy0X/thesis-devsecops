"""The ways a request to the model gateway can fail, each with a stable ``code`` (the OpenAI error shape: ``{"error": {"code", "message"}}``) and the HTTP status it is answered with."""
from __future__ import annotations

STATUS = {
    "cancelled": 503,
    'gpu_required': 503,
    "llama_cpp_missing": 503,       # the llama.cpp server program is not installed (the status carries an install action)
    "model_file_missing": 404,      # the chosen model file is not on disk
    "does_not_fit": 507,            # the model fits nowhere on this computer (not on the card, not in main memory)
    "load_timeout": 504,            # the server did not answer within the load time
    "load_failed": 502,             # the server stopped while loading, or could not be started
    "gpu_exclusive": 503,           # a training run holds the card; the gateway answers again when it ends
    "busy": 503,                    # the request waited its longest for the model it needs
    "bad_request": 400,
    "unauthorized": 401,
    "not_configured": 409,          # no model is chosen for this role
    "not_local": 409,               # the local role resolved to a remote inference service
    "needs_consent": 409,           # the model loads only with a change that lowers its quality, which the quality policy does not allow
    "context_length_exceeded": 400, # the prompt does not fit the context the model runs with
    "tool_protocol_failed": 502,   # an explicit model transport returned no valid complete tool/answer decision
}


class GatewayError(RuntimeError):
    """A failure with a code and a sentence for the person. ``retry_after`` (seconds) is set when waiting and asking again is the right answer."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None, action: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after, self.action, self.details = code, message, retry_after, action, details

    @property
    def status(self) -> int:
        return STATUS.get(self.code, 500)

    def body(self) -> dict:
        err = {"code": self.code, "message": self.message, "type": "gateway_error"}
        if self.action:
            err["action"] = self.action
        if self.details:
            err["details"] = self.details
        return {"error": err}
