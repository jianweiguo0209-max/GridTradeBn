import time


def test_timezone_is_pinned():
    # conftest 应把进程时区钉到东八区（UTC+8 => +28800 秒）
    assert time.localtime().tm_gmtoff == 8 * 3600


def test_packages_importable():
    import gridtrade.core  # noqa: F401
    import gridtrade.exchanges  # noqa: F401
