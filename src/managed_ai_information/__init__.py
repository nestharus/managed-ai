"""Managed AI shared runtime, store, deployment, and library package."""

__version__ = "0.5.0"
ENGINE_API = "managed-ai-runtime/1"
STORE_SCHEMA = "managed-ai-store/1"
DEPLOYMENT_API = "agent-system-deployment/2"

__all__ = [
    "DEPLOYMENT_API",
    "ENGINE_API",
    "STORE_SCHEMA",
    "__version__",
]
