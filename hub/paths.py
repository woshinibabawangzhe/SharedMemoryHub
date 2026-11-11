"""Portable asset and write boundaries; reject links and implicit path rewrites."""
from pathlib import Path
import os

PROJECT_ROOT = Path(os.path.abspath(__file__)).parents[1]


def _without_links(path):
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, 'is_junction') and ancestor.is_junction()):
            raise ValueError('reparse_point_forbidden')
    return path


def checked_asset(value):
    """Read-only assets live in the checkout or one explicitly configured root."""
    p = _without_links(Path(os.path.abspath(value)))
    roots = [PROJECT_ROOT]
    configured = os.environ.get('SMH_ASSET_ROOT')
    if configured:
        asset_root = Path(configured)
        if not asset_root.is_absolute():
            raise ValueError('invalid_asset_root')
        asset_root = Path(os.path.abspath(asset_root))
        if asset_root == Path(asset_root.anchor):
            raise ValueError('invalid_asset_root')
        roots.append(_without_links(asset_root))
    if not any(p.is_relative_to(allowed) for allowed in roots):
        raise ValueError('asset_outside_approved_runtime')
    return p


def checked_path(value):
    """All writes stay inside the actual checkout, regardless of its drive."""
    _without_links(PROJECT_ROOT)
    p = _without_links(Path(os.path.abspath(value)))
    if not p.is_relative_to(PROJECT_ROOT):
        raise ValueError('path_outside_shared_memory_hub')
    return p
