"""Floyo Video Studio — a single, hosted-safe video helper node for ComfyUI.

Frontend assets live in ./web/js (auto-loaded by ComfyUI via WEB_DIRECTORY).
"""

from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web/js"

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]
