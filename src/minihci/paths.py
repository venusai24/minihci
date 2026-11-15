import os
from pathlib import PurePosixPath


def resolve_image(path: str, root: str) -> str:
    """Return the real path of `path` if it is absolute, has no `..` and lies under `root`.

    Filesystem/string check only (D-014); raises ValueError otherwise.
    """
    if not os.path.isabs(path):
        raise ValueError("image path must be absolute")
    if ".." in PurePosixPath(path).parts:
        raise ValueError("image path must not contain '..'")
    real = os.path.realpath(path)
    real_root = os.path.realpath(root)
    if real == real_root or os.path.commonpath([real, real_root]) != real_root:
        raise ValueError(f"image path must be under {real_root}")
    return real
