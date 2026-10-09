from datetime import date, datetime

from scripts import short_term_daily as module


def test_delayed_preopen_does_not_publish(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 9, 9, 35, tzinfo=tz)

    def unexpected(*args, **kwargs):
        raise AssertionError('delayed preopen must not scan, write, or notify')

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'trading_dates', lambda _: (True, date(2026, 10, 8)))
    monkeypatch.setenv('QUANTI_SESSION', 'preopen')
    monkeypatch.setattr(module, 'scan_preopen', unexpected)
    monkeypatch.setattr(module, 'scan', unexpected)
    monkeypatch.setattr(module, '_load_state', unexpected)
    monkeypatch.setattr(module, 'notify_wecom', unexpected)
    module.main()
