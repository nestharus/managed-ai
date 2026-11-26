"""The standalone and native packet exchange share one store binding and effect authority."""
from .packet_context import PacketContext
from .carrier_receipts import TransportReceipts
from .receive_return import PacketReceiving
from .publication import TargetPublication
from .inspection import PacketInspection


class Exchange(PacketContext, PacketReceiving, TransportReceipts, TargetPublication, PacketInspection):
    """One cooperative packet owner. Embedded callers serialize effects with the owner lock."""
