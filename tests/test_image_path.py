import pytest

from minihci.paths import resolve_image


def test_ok_path(tmp_path):
    assert resolve_image(str(tmp_path / "a.qcow2"), str(tmp_path)) == str(tmp_path.resolve() / "a.qcow2")


def test_relative_rejected(tmp_path):
    with pytest.raises(ValueError):
        resolve_image("a.qcow2", str(tmp_path))


def test_dotdot_rejected(tmp_path):
    (tmp_path / "sub").mkdir()
    with pytest.raises(ValueError):
        resolve_image(str(tmp_path / "sub" / ".." / "a.qcow2"), str(tmp_path))


def test_outside_root_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for p in (tmp_path / "other.qcow2", tmp_path / "root2" / "a", root):
        with pytest.raises(ValueError):
            resolve_image(str(p), str(root))


def test_symlink_out_of_root_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret").write_text("x")
    (root / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError):
        resolve_image(str(root / "link"), str(root))
