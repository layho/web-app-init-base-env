"""Isolated V0.6.0 event engine. Production protocol stays disabled.

The generic event outbox consumer queues accepted events in the same
SQLite transaction as the event. It does not call a production route.
Report delivery remains a separate isolated outbox.
"""

from .engine import FOLLOW_UPS, PACKAGE_COMPLETE, Engine
from .identity import PRODUCTION_PROTOCOL_ENABLED

__all__ = ["Engine", "FOLLOW_UPS", "PACKAGE_COMPLETE", "PRODUCTION_PROTOCOL_ENABLED"]
