import sys
import time
from datetime import datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows desktop')


def test_desktop_persists_without_network_and_deduplicates(tmp_path):
    from quanti.desktop import Engine, BEIJING
    calls = []
    def fetch(code):
        calls.append(code)
        raise ConnectionError()
    engine = Engine(tmp_path, fetch=fetch, start_worker=False)
    try:
        started = time.perf_counter()
        engine.submit('buy', '603083', .5)
        assert time.perf_counter() - started < 1
        assert not calls
        engine.submit('buy', '603083', .5)
        assert len(engine.state['orders']) == 1
        assert engine.state['cash'] == 100000
        engine.running = True
        engine.tick()
        assert engine.state['orders'][0]['status'] == 'pending'
        assert not engine.state['trades']
        engine.submit('cancel', '603083', .5)
        assert engine.state['orders'][0]['status'] == 'cancelled'
        assert datetime.now(BEIJING) > datetime.fromisoformat(engine.state['orders'][0]['created_at'])
    finally:
        engine.close()


def test_desktop_fill_after_confirmation_and_restart(tmp_path):
    from quanti.desktop import Engine, BEIJING
    now = datetime.now(BEIJING).replace(hour=10, minute=0, second=0, microsecond=0)
    def fetch(code):
        return dict(code=code, name='测试股票', price=10, bid=10, ask=10,
                    bid_lots=10000, ask_lots=10000, volume=10000, limit_up=11, limit_down=9,
                    time=now.isoformat(), source='fixture')
    engine = Engine(tmp_path, fetch=fetch, start_worker=False)
    try:
        engine.submit('buy', '603083', .5)
        order = engine.state['orders'][0]
        order['created_at'] = (now - timedelta(seconds=5)).isoformat()
        order['expires_at'] = (now + timedelta(hours=1)).isoformat()
        engine.paper.match_order(engine.state, order, fetch('603083'), now)
        assert order['status'] == 'filled'
        assert len(engine.state['trades']) == 1
        from quanti.desktop import write_json
        write_json(engine.path, engine.state)
    finally:
        engine.close()
    restarted = Engine(tmp_path, start_worker=False)
    try:
        assert not restarted.running
        assert len(restarted.state['trades']) == 1
        assert restarted.state['positions']['603083']['quantity'] > 0
    finally:
        restarted.close()


def test_windows_settings_encrypted_at_rest(tmp_path):
    from quanti.desktop_settings import load_settings, save_settings
    values = {'LLM_PRIMARY_API_KEY': 'TEST-NOT-A-REAL-SECRET'}
    save_settings(values, tmp_path)
    assert b'TEST-NOT-A-REAL-SECRET' not in (tmp_path / 'settings.dpapi').read_bytes()
    assert load_settings(tmp_path) == values
