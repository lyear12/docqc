"""docqc — document quality checker for academic deliverables.

The package is layered so each level can be understood and tested on its own::

    models     vocabulary: Finding / CheckResult / Report  (no dependencies)
    ooxml      raw WordprocessingML helpers                (no dependencies)
    document   format-neutral view of the input            (uses models, ooxml)
    render     docx -> pdf -> png                          (uses document)
    registry   checker plugin protocol                     (uses models)
    checkers/  one module per check                        (uses all of the above)
    runner     runs checkers, assembles the report         (uses registry)
    cli        the ``docqc`` command                       (uses runner)
    web        FastAPI app + React UI                      (uses runner)

Importing this package must stay cheap: nothing here pulls in ``python-docx``,
``fitz``, or ``fastapi`` at import time. The CLI starts in a few milliseconds
because of that.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
