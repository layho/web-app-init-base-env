"""Re-export the shared default-deny matrix."""

from app.control_authz.authorize import MATRIX, authorize

__all__ = ["MATRIX", "authorize"]
