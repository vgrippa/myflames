"""Build the backend on the target OS using an explicit PyInstaller install.

Run from a disposable build venv. This never installs into the user's Python.
"""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[3]
desktop = root / 'apps' / 'desktop'
command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
           '--onedir', '--name', 'myflames-backend', '--paths', str(root),
           '--collect-all', 'myflames', '--distpath', str(desktop / 'build' / 'backend'),
           '--workpath', str(desktop / 'build' / 'pyinstaller'),
           '--specpath', str(desktop / 'build'),
           str(desktop / 'scripts' / 'backend-entry.py')]
raise SystemExit(subprocess.call(command, cwd=str(root)))
