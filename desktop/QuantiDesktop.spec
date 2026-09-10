# Build on Windows: python -m PyInstaller --noconfirm desktop/QuantiDesktop.spec
from pathlib import Path
import shutil
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH).parent
node = shutil.which('node')
if not node:
    raise RuntimeError('Install Node.js before building the desktop distribution')
datas = [(str(root / 'config/short_term.json'), 'config'), (str(root / 'LICENSE'), 'licenses')]
for package in ['akshare', 'pywencai']:
    datas += collect_data_files(package) + copy_metadata(package)
a = Analysis([str(root / 'scripts/desktop_launcher.py')], pathex=[str(root)],
             binaries=[(node, 'node')], datas=datas, hiddenimports=['scripts.short_term_daily'],
             hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='QuantiDesktop', console=False)
coll = COLLECT(exe, a.binaries, a.datas, name='QuantiDesktop')
