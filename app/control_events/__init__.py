"""Isolated V0.6.0 event engine. Production protocol stays disabled.

The generic event outbox consumer (D07) is not delivered. Event rows stay
``not_delivered``. This package is not complete until that consumer lands
in a later W01 change. Report delivery is a separate isolated outbox.
"""

from .engine import FOLLOW_UPS, PACKAGE_COMPLETE, Engine
from .identity import PRODUCTION_PROTOCOL_ENABLED

__all__ = ["Engine", "FOLLOW_UPS", "PACKAGE_COMPLETE", "PRODUCTION_PROTOCOL_ENABLED"]
