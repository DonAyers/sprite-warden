"""Local asset gallery: an on-demand viewer over the catalog and the project's art tree.

``warden serve`` starts a stdlib HTTP server (no web framework) that renders the
``AssetCatalog`` rows, the ``<image>.gate.json`` sidecars, ``pipeline.json`` routes,
character identities and the rejection log as one browsable page, and serves the
image bytes themselves. ``warden sync`` walks the project and indexes every image
(and its gate record) into the catalog first, so nothing on disk is invisible.

Everything here is read-only; it never writes to the art tree.
"""

from __future__ import annotations

from .data import GalleryData
from .sync import SyncReport, sync_paths

__all__ = ["GalleryData", "SyncReport", "sync_paths"]
