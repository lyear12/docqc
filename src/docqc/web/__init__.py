"""Web layer: FastAPI app, job store, and the SSE progress stream.

The API exists so the checking logic already proven by the CLI can be driven
from a browser. It adds no new checking behaviour — every verdict still comes
from :mod:`docqc.runner`, and the JSON returned is the same ``Report`` the CLI
writes to disk. Keeping the web layer thin is what stops the CLI and the UI from
disagreeing about what a document's problems are.

Layout::

    schemas.py  request/response models (the wire contract)
    jobs.py     in-memory job store + background execution
    app.py      routes and the app factory
"""

from __future__ import annotations

__all__: list[str] = []
