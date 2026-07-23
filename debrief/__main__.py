"""Console entry point: `debrief` (and `python -m debrief`).

Ensures the vault exists, prints the local URL, then serves the FastAPI app on
127.0.0.1:8377. The app itself lives in app.py at the repo root; we import it by
module name so `python app.py` keeps working unchanged.
"""

from __future__ import annotations

import sys

from . import config, vault

APP_HOST = "127.0.0.1"
APP_PORT = 8377


def main() -> None:
    try:
        vault.ensure_vault()
    except Exception:
        # A missing vault is reported by /api/status; never block startup.
        pass

    # app.py sits at the repo root (a top-level module, not part of the package).
    # Put the repo root on sys.path so "app:app" imports whether we are launched
    # by `python app.py`, `python -m debrief`, or the installed `debrief` script.
    repo_root = str(config.REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    import uvicorn

    print(f"Debrief is running at http://{APP_HOST}:{APP_PORT}")
    uvicorn.run("app:app", host=APP_HOST, port=APP_PORT)


if __name__ == "__main__":
    main()
