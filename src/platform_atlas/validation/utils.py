"""Utilities for Validation Engine"""

import time
import logging
from functools import lru_cache
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Module-level session for connection pooling
_session = None

logger = logging.getLogger(__name__)

# This will stay in-place but set to False for production code.
# This is helpful for repeated testing without sending a lot
# of requests to gitlab.
DEBUG_MODE = False
MOCK_ADAPTER_VERSIONS = {
    "adapter-viptela": "0.19.0",
    "adapter-db_mongo": "0.11.1",
    "adapter-nautobot": "0.8.0",
    "adapter-phpipam": "2.7.0"
}
REQUESTS_PER_SECOND = 2.0

class _TimeoutHTTPAdapter(HTTPAdapter):
    """HTTPAdapter with a default timeout"""
    DEFAULT_TIMEOUT = 30

    def send(self, *args, **kwargs):
        kwargs.setdefault("timeout", self.DEFAULT_TIMEOUT)
        return super().send(*args, **kwargs)

def get_gitlab_session() -> requests.Session:
    """Get or create a session with connection pooling and retry logic"""
    global _session
    if _session is None:
        _session = requests.Session()

        # Connection pooling
        adapter = _TimeoutHTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=Retry(
                total=3,
                backoff_factor=1,
                status_forcelist=[429, 500, 502, 503, 504],
            )
        )
        _session.mount("https://", adapter)

    return _session

class RateLimiter:
    """Simple token bucket rate limiter"""

    def __init__(self, requests_per_second: float = 2.0):
        self.requests_per_second = requests_per_second
        self.min_interval = 1.0 / requests_per_second
        self.last_request_time = 0.0

    def __repr__(self) -> str:
        return f"<RateLimiter rps={self.requests_per_second}>"

    def wait(self):
        """Wait if necessary to maintain rate limit"""
        now = time.time()
        time_since_last = now - self.last_request_time

        if time_since_last < self.min_interval:
            sleep_time = self.min_interval - time_since_last
            time.sleep(sleep_time)

        self.last_request_time = time.time()

# Module-level rate limiter
_rate_limiter = RateLimiter(requests_per_second=REQUESTS_PER_SECOND)

@lru_cache(maxsize=256)
def get_latest_version(adapter_name: str) -> str:
    """
    Fetch the latest version tag for an Itential open-source
    adapter from Gitlab.
    """
    if DEBUG_MODE:
        logger.debug("[MOCK] Returning fake version for '%s'", adapter_name)
        return MOCK_ADAPTER_VERSIONS.get(adapter_name, "99.99.99")

    _rate_limiter.wait() # Enforce rate limit

    # Itential Gitlab Endpoint
    project_path = f"itentialopensource/adapters/{adapter_name}"
    encoded_path = requests.utils.quote(project_path, safe="")
    url = f"https://gitlab.com/api/v4/projects/{encoded_path}/repository/tags"

    logger.debug("GET %s (adapter=%s)", url, adapter_name)

    session = get_gitlab_session()
    response = session.get(
        url,
        params={"per_page": 1, "order_by": "version"},
        timeout=30
    )

    logger.debug(
        "Response: %s %s (%.0fms, %d bytes)",
        response.status_code,
        response.reason,
        response.elapsed.total_seconds() * 1000,
        len(response.content)
    )

    if not response.ok:
        logger.debug("Response body: %s", response.text[:500])

    response.raise_for_status()

    tags = response.json()
    if not tags:
        raise ValueError(f"No tags found for {adapter_name}")

    version = str(tags[0]["name"])
    logger.debug("Resolved %s -> %s", adapter_name, version)
    return version
