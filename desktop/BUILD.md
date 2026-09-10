# Windows desktop build

Use Windows x64, Python 3.12 and Node.js 24. From the repository root:

```powershell
python -m pip install -e ".[dev]" pywencai==0.13.1 pyinstaller==6.22.2
python -m pytest tests/test_desktop.py tests/test_short_term_user.py tests/test_tencent_quotes.py tests/test_commission.py -q
python -m PyInstaller --noconfirm desktop/QuantiDesktop.spec
dist/QuantiDesktop/QuantiDesktop.exe --smoke-test
```

`--smoke-test` uses a temporary empty account and never trades. `--diagnostics-file PATH` checks packaged JavaScript, current-user encrypted settings presence, a read-only quote and Wencai access. It writes only status/count/timing information, never credentials or account changes.

Bundle the complete onedir output, the Chinese user guide, the Quanti MIT license, Python/package licenses and the bundled Node version's official LICENSE. Keep `settings.dpapi`, `account.json`, migration backups, private keys and all personal reports OUT of the release archive. They live separately in `%LOCALAPPDATA%/QuantiDesktop`.

The desktop and cloud paper engines must not execute the same account at the same time. The current deployment pauses cloud `paper-trade.yml`; cloud stock research may continue. Desktop starts monitoring paused and persists confirmed orders locally before fetching quotes. It is a paper system, not a live broker adapter.
