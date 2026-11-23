"""Shared decimal-byte limits for manual JSON exchange."""
DEFAULT_BYTES = 40_000_000
MAX_SAFE_BYTES = 2**53 - 1  # Exact across the Python API and JavaScript UI.


def file_bytes(value):
    if type(value) is not int or not 0 < value <= MAX_SAFE_BYTES:
        raise ValueError('max_file_bytes must be a positive integer within the exact JavaScript byte range')
    return value
