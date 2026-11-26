"""The standalone and native packet exchange share one store binding and effect authority."""

from .carrier_receipts import TransportReceipts
from .inspection import PacketInspection
from .packet_context import PacketContext
from .publication import TargetPublication
from .receive_return import PacketReceiving


class Exchange(
    PacketContext, PacketReceiving, TransportReceipts, TargetPublication, PacketInspection
):
    """One cooperative packet owner. Embedded callers serialize effects with the owner lock."""
