"""Retry decorator for transient failures."""
import functools
import logging
import time

logger = logging.getLogger(__name__)


def retry(times: int = 3, delay_seconds: float = 2.0, backoff: float = 2.0,
          exceptions: tuple = (IOError, OSError)):
    """Retry a function on transient errors with exponential backoff.

    delay pattern with defaults: 2s, 4s, 8s — then give up and re-raise.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay_seconds
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    if attempt == times:
                        logger.error("%s failed after %d attempts: %s",
                                     fn.__name__, times, exc)
                        raise
                    logger.warning("%s attempt %d/%d failed (%s); retrying in %.0fs",
                                   fn.__name__, attempt, times, exc, wait)
                    time.sleep(wait)
                    wait *= backoff
        return wrapper
    return decorator