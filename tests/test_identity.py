"""DugUp's identity to Windows (identity.py): the app ID and the icon on every window."""

from dugup import identity


def test_the_icon_ships_with_the_package_and_holds_the_sizes_windows_uses():
    from PIL import Image
    assert identity.ICON.is_file()
    with Image.open(identity.ICON) as icon:
        assert {(16, 16), (32, 32), (48, 48), (256, 256)} <= set(icon.info["sizes"])
    small = identity.icon_image(64)
    assert small.size == (64, 64) and small.getpixel((0, 0))[3] == 0, "the corners are transparent"


def test_the_app_id_is_claimed_only_on_windows():
    assert identity.claim_app_id("linux") is False


def test_dressing_a_window_never_fails_and_gives_it_the_icon():
    from test_window import make_root
    root = make_root()
    try:
        identity.dress(root, "linux")
        assert getattr(root, "_dugup_icon", None) is not None
        identity.dress(object(), "win32")          # not a window at all: still no error
    finally:
        root.destroy()
