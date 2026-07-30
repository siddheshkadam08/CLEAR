"""Contract Intelligence Platform backend.

All business logic lives in this package. The Node/BullMQ layer is a logic-free
dispatch shim that calls the internal stage endpoints exposed by
:mod:`app.workers`.
"""

__version__ = "1.0.0"
