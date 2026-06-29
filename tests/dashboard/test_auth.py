from gridtrade.dashboard.auth import (hash_password, verify_password,
                                      make_session, verify_session, LoginThrottle)


def test_password_hash_roundtrip():
    enc = hash_password('s3cret', iterations=1000)
    assert verify_password('s3cret', enc) is True
    assert verify_password('wrong', enc) is False
    assert verify_password('s3cret', 'garbage') is False


def test_session_roundtrip_and_expiry():
    tok = make_session('admin', 'topsecret', ttl_sec=100, now_fn=lambda: 1000)
    assert verify_session(tok, 'topsecret', now_fn=lambda: 1050) == 'admin'
    assert verify_session(tok, 'topsecret', now_fn=lambda: 2000) is None     # 过期
    assert verify_session(tok, 'wrongsecret', now_fn=lambda: 1050) is None   # 验签失败
    assert verify_session('not.a.token', 'topsecret') is None


def test_throttle_locks_after_max_attempts():
    t = [1000.0]
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: t[0])
    for _ in range(3):
        assert thr.is_locked('admin') is False
        thr.record_failure('admin')
    assert thr.is_locked('admin') is True            # 达上限 -> 锁定
    t[0] += 3599
    assert thr.is_locked('admin') is True            # 1h 内仍锁
    t[0] += 2
    assert thr.is_locked('admin') is False           # 超 1h 解锁


def test_throttle_success_resets():
    thr = LoginThrottle(max_attempts=3, lockout_sec=3600, now_fn=lambda: 1000.0)
    thr.record_failure('admin')
    thr.record_failure('admin')
    thr.record_success('admin')
    thr.record_failure('admin')
    assert thr.is_locked('admin') is False           # 计数已被成功清零
