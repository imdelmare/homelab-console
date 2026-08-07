"""Normalized provider error taxonomy.

Provider implementations must raise ProviderError with one of the codes
below. Messages must never contain credentials or authorization headers.
"""

from typing import Literal

ProviderErrorCode = Literal[
    "configuration_missing",
    "credentials_missing",
    "auth_failed",
    "timeout",
    "tls_error",
    "unreachable",
    "permission_denied",
    "invalid_input",
    "invalid_response",
    "degraded",
]


class ProviderError(Exception):
    def __init__(self, code: ProviderErrorCode, message: str) -> None:
        self.code: ProviderErrorCode = code
        self.message = message
        super().__init__(f"{code}: {message}")
