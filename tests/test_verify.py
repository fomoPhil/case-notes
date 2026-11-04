"""The screen check keeps no screenshot on disk. Capture and model are mocked."""

import os

from debrief import llm, verify


def test_screenshot_is_deleted_after_the_model_reads_it(monkeypatch):
    seen = {}

    def fake_capture(path):
        with open(path, "wb") as fh:
            fh.write(b"png")
        seen["path"] = path
        return True

    def fake_chat(messages, schema=None, images=None, **kw):
        seen["existed_during_read"] = os.path.exists(images[0])
        seen["mode"] = oct(os.stat(images[0]).st_mode & 0o777)
        return {"confirmed": True, "what_i_see": "an event"}

    monkeypatch.setattr(verify, "_activate", lambda app: None)
    monkeypatch.setattr(verify.time, "sleep", lambda s: None)
    monkeypatch.setattr(verify, "_capture_and_downscale", fake_capture)
    monkeypatch.setattr(llm, "chat", fake_chat)

    out = verify.verify_on_screen([{"surface": "calendar", "question": "q"}])
    assert out[0]["confirmed"] is True
    assert seen["existed_during_read"] is True
    assert seen["mode"] == "0o600"
    assert not seen["path"].startswith("/tmp/debrief_verify.png")
    assert not os.path.exists(seen["path"])


def test_screenshot_is_deleted_when_the_model_fails(monkeypatch):
    seen = {}

    def fake_capture(path):
        seen["path"] = path
        return True

    def boom(*a, **k):
        raise llm.ModelUnavailable("down")

    monkeypatch.setattr(verify, "_activate", lambda app: None)
    monkeypatch.setattr(verify.time, "sleep", lambda s: None)
    monkeypatch.setattr(verify, "_capture_and_downscale", fake_capture)
    monkeypatch.setattr(llm, "chat", boom)

    out = verify.verify_on_screen([{"surface": "mail", "question": "q"}])
    assert out[0]["confirmed"] is False
    assert not os.path.exists(seen["path"])
