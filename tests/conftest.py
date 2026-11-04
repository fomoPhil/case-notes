"""Shared test setup.

Starlette's TestClient sends Host: testserver. The app only answers to local
host names, so the test host is added here rather than in production code.
"""

import app as _app

_app.ALLOWED_HOSTS.add("testserver")
