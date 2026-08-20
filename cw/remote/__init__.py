"""Optional production-oriented CW remote gateway and outbound agent.

The remote package is an adapter boundary.  It deliberately does not live in
``cw.core`` or ``cw.application`` and ordinary local CW imports do not require
its network, OAuth, or cryptography dependencies.
"""

from .errors import RemoteError, RemoteErrorCode
from .protocol import PROTOCOL_VERSION, RemoteIdentity, RemoteRequest, RemoteResponse

__all__ = [
    "PROTOCOL_VERSION",
    "RemoteError",
    "RemoteErrorCode",
    "RemoteIdentity",
    "RemoteRequest",
    "RemoteResponse",
]
