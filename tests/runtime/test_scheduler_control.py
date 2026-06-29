from gridtrade.runtime.scheduler import run_scheduler_once


class _Flags:
    def __init__(self, halted=False, paused=False): self._h = halted; self._p = paused
    def get(self, name):
        return {'trading_halted': self._h, 'scheduler_paused': self._p}.get(name, False)


class _HB:
    def __init__(self): self.beats = []
    def beat(self, m): self.beats.append(m)


class _RT:
    def __init__(self, flags):
        self.flags = flags; self.heartbeats = _HB()
        self.config = type('C', (), {'exchange': 'fake'})()


def test_scheduler_skips_when_paused():
    rt = _RT(_Flags(paused=True))
    out = run_scheduler_once(rt, now_fn=lambda: 0.0)
    assert out.get('skipped') == 'paused'
    assert rt.heartbeats.beats == ['scheduler']         # 心跳照常


def test_scheduler_skips_when_halted():
    rt = _RT(_Flags(halted=True))
    out = run_scheduler_once(rt, now_fn=lambda: 0.0)
    assert out.get('skipped') == 'halted'
