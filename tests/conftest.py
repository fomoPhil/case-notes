"""Shared test setup.

Starlette's TestClient sends Host: testserver. The app only answers to local
host names, so the test host is added here rather than in production code.

Non-live tests never reach a model server. Without this, any test that runs
the summary step would quietly call LM Studio whenever it happened to be open,
and pass or fail on what the model said. Tests that need a canned reply still
monkeypatch requests.post or llm.chat themselves, which wins over this.
"""

import pytest
import requests

import app as _app

_app.ALLOWED_HOSTS.add("testserver")


@pytest.fixture(autouse=True)
def _no_model_server(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return

    def refuse(*args, **kwargs):
        raise requests.ConnectionError("non-live tests do not call a model server")

    monkeypatch.setattr(requests, "post", refuse)
