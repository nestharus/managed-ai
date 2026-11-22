"""Native work, retained evidence and effect admission for the agent runtime."""
from pathlib import Path

# Installed verifiers pin the domain and catalogue loaders by ordinary file path.
__path__.append(str(Path(__file__).resolve().parents[3] / 'prototype/agent-system/native_runtime'))
