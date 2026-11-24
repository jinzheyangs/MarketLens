"""MarketScope backend application package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("marketscope-backend")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]
