"""本项目写入边界；拒绝重解析点，不隐式改写路径。"""
from pathlib import Path
import os

PROJECT_ROOT = Path(os.path.abspath(__file__)).parents[1]


def checked_asset(value):
    """共享的模型/解释器只读入口；不授予项目外写入权限。"""
    p = Path(os.path.abspath(value))
    if os.name == 'nt' and not p.is_relative_to(Path('E:/AI运行区')):
        raise ValueError('asset_outside_approved_runtime')
    for ancestor in (p, *p.parents):
        if ancestor.exists() and (ancestor.is_symlink() or ancestor.is_junction()):
            raise ValueError('reparse_point_forbidden')
    return p


def checked_path(value):
    checked_asset(PROJECT_ROOT)
    p = Path(os.path.abspath(value))
    if not p.is_relative_to(PROJECT_ROOT):
        raise ValueError('path_outside_shared_memory_hub')
    for ancestor in (p, *p.parents):
        if ancestor.exists() and (ancestor.is_symlink() or ancestor.is_junction()):
            raise ValueError('reparse_point_forbidden')
    return p
