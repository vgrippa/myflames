"""Build the backend on the target OS using an explicit PyInstaller install.

Run from a disposable build venv. This never installs into the user's Python.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

root = Path(__file__).resolve().parents[3]
desktop = root / 'apps' / 'desktop'
if importlib.util.find_spec('PyInstaller') is None:
    # `npm run build:backend` starts the system python3. Use the documented
    # disposable build venv when it exists instead of failing on a bare import.
    venv = root / 'local' / 'desktop-build-venv'
    venv_python = venv / 'bin' / 'python'
    if venv_python.is_file() and os.path.realpath(sys.prefix) != os.path.realpath(str(venv)):
        raise SystemExit(subprocess.call([str(venv_python), str(Path(__file__).resolve())], cwd=str(root)))
    sys.stderr.write(
        'PyInstaller is not installed for {}.\n'
        'Create the build venv first:\n'
        '  python3 -m venv local/desktop-build-venv\n'
        '  local/desktop-build-venv/bin/python -m pip install -r apps/desktop/requirements-build.txt\n'.format(sys.executable))
    raise SystemExit(2)
# Collect submodules and package data, not distribution metadata: a leftover
# development myflames.egg-info in the checkout would otherwise ship with a
# stale version. Modules are compiled into the archive.
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
           '--onedir', '--name', 'myflames-backend', '--paths', str(root),
           '--collect-submodules', 'myflames', '--collect-data', 'myflames',
           '--distpath', str(desktop / 'build' / 'backend'),
           '--workpath', str(desktop / 'build' / 'pyinstaller'),
           '--specpath', str(desktop / 'build'),
           str(desktop / 'scripts' / 'backend-entry.py')]
status = subprocess.call(command, cwd=str(root))
if status == 0:
    # The bundled interpreter's license ships beside the app's own notices.
    shutil.copyfile(str(Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'),
                    str(desktop / 'build' / 'backend' / 'Python-LICENSE.txt'))
raise SystemExit(status)
