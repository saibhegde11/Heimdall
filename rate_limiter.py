import time
from threading import Lock
from config import get_settings

settings = get_settings()
CAPACITY = settings.rate_limit_capacity
REFILL_RATE = settings.rate_limit_refill_rate

buckets = {}
bucket_lock = Lock()


def refill_tokens(
    tokens: float,
    elapsed: float,
    capacity: int,
    refill_rate: float,
) -> float:
    added_tokens = elapsed * refill_rate
    return min(capacity, tokens + added_tokens)


def allow_request(client_ip: str) -> tuple[bool, float]:
    """Spend one token if available; otherwise return the refill wait in seconds."""
    with bucket_lock:
        now = time.monotonic()
        bucket = buckets.get(client_ip)

        if bucket is None:
            bucket = {
                "tokens": float(CAPACITY),
                "last_refill": now,
            }
            buckets[client_ip] = bucket

        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = refill_tokens(
            tokens=bucket["tokens"],
            elapsed=elapsed,
            capacity=CAPACITY,
            refill_rate=REFILL_RATE,
        )
        bucket["last_refill"] = now

        if bucket["tokens"] >= 1.0:
            bucket["tokens"] -= 1.0
            return True, 0.0

        retry_after = (1.0 - bucket["tokens"]) / REFILL_RATE
        return False, retry_after
