"""Cached, rate-limited HTTP fetch layer — Work order 4 (Milestone 3). Not yet implemented.

Will provide get(url) -> CachedResponse: cache to cache/{sha256(url)}.json,
per-domain min interval 1.0s, real UA, follow redirects, 10s timeout.
Playwright fallback arrives in Milestone 4.
"""
