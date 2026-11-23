class EmptyStreamError(RuntimeError):
    """Raised when a provider closes a stream without yielding a response."""


class MoAPresetNotFoundError(ValueError):
    """Raised when a persisted MoA preset no longer exists in config."""


class InvalidProviderResponseError(RuntimeError):
    """An HTTP-200 answer with no usable ``choices[0].message``: the route cannot answer this request.
    The provider's own ``error`` object, when the body carried one, rides ``provider_error``."""


class CredentialPoolUnavailableError(RuntimeError):
    """No usable credential for the request: every pooled credential for the provider is exhausted
    or in a cooldown for the model. Raised before the SDK is handed an empty key (which fails with
    an unrelated ``TypeError`` about the auth method); a rate-limit verdict that never touches the
    pool, so the turn falls back or waits out the named cooldown instead."""

    def __init__(self, message: str, *, provider: str | None = None, model: str | None = None):
        super().__init__(message)
        self.provider, self.model = provider, model
