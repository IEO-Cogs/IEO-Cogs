class FetchError(Exception):
    """Custom exception for fetching errors."""


class GameNotFoundError(FetchError):
    """Exception for game not found errors."""


class StreamFetchError(FetchError):
    """Exception for stream fetch errors."""
