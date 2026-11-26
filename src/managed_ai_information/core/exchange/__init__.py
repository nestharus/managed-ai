"""Public embedding API and the portable g60-work entry point."""

from typing import Any

from .api import Exchange
from .packet_context import MAX_PACKET_BYTES
from .publication import PublicationBasisChanged

__all__ = ["MAX_PACKET_BYTES", "Exchange", "PublicationBasisChanged", "main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        from .cli import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
