"""Pytest configuration: add repo root to path and stub Hermes framework modules."""

import sys
import types
from pathlib import Path

# Ensure repo root is on sys.path so `tools.*` imports resolve
sys.path.insert(0, str(Path(__file__).parent))

# Stub out tools.registry (provided by hermes-agent at runtime, not installed locally)
registry_mod = types.ModuleType("tools.registry")


class _FakeRegistry:
    def register(self, *args, **kwargs):
        pass


registry_mod.registry = _FakeRegistry()
sys.modules["tools.registry"] = registry_mod
