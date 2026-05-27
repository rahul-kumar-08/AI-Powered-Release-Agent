"""
Typed exception hierarchy for Release Agent tools.

All tool functions raise these exceptions so the orchestrator
(agent_runner.py) can distinguish transient failures from permanent
ones and decide whether to retry.
"""


class ToolError(Exception):
    """Base for all tool errors. Carries a human-readable message."""

    def __init__(self, message, *, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class AuthError(ToolError):
    """Missing or invalid credentials (401/403). Never retryable."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message, retryable=False, status_code=status_code or 401)


class ConfigError(ToolError):
    """Missing environment variable or bad configuration. Never retryable."""

    def __init__(self, message):
        super().__init__(message, retryable=False)


class RateLimitError(ToolError):
    """API rate limit hit (429). Always retryable after wait."""

    def __init__(self, message, *, retry_after=None, status_code=429):
        super().__init__(message, retryable=True, status_code=status_code)
        self.retry_after = retry_after


class HttpError(ToolError):
    """Non-retryable HTTP error (4xx other than 401/403/429)."""

    def __init__(self, message, *, status_code):
        retryable = status_code in (502, 503, 504)
        super().__init__(message, retryable=retryable, status_code=status_code)


class NetworkError(ToolError):
    """DNS, timeout, connection refused. Always retryable."""

    def __init__(self, message):
        super().__init__(message, retryable=True)


class NotFoundError(ToolError):
    """Resource not found (404). Never retryable."""

    def __init__(self, message):
        super().__init__(message, retryable=False, status_code=404)


class DataError(ToolError):
    """Bad input data, parse failure, or empty result set. Never retryable."""

    def __init__(self, message):
        super().__init__(message, retryable=False)
