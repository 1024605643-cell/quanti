"""Adapt pywencai's console helper and timeouts for the desktop runtime."""
import importlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import requests

last_error = ''


def configure_runtime():
    global last_error
    last_error = ''
    module = importlib.import_module('pywencai.wencai')
    headers = importlib.import_module('pywencai.headers')
    session = requests.Session()

    def request(method, url, **kwargs):
        global last_error
        kwargs.setdefault('timeout', (5, 15))
        response = session.request(method, url, **kwargs)
        if response.status_code >= 400:
            last_error = f'问财接口拒绝或未完成请求（HTTP {response.status_code}），未参与本次加分'
        response.raise_for_status()
        return response

    module.rq = SimpleNamespace(request=request)
    if os.name == 'nt':
        def get_token():
            # A windowed executable has no inherited console handles.
            # Explicitly pipe all three handles and hide this Node helper.
            result = subprocess.run(
                ['node', str(Path(headers.__file__).with_name('hexin-v.bundle.js'))],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=20, check=True)
            token = result.stdout.decode().strip()
            if not token:
                raise RuntimeError('Wencai JavaScript helper returned no token')
            return token
        headers.get_token = get_token
