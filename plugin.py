"""Host discovery only; no installation, patching or runtime IO."""

from .neko_files.plugin import create_plugin

__all__ = ["create_plugin"]
