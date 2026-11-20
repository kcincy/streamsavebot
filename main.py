"""Ponto de entrada unico: python main.py."""
from pathlib import Path
import os
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))


def main():
    venv = ROOT / '.venv'
    if Path(sys.prefix).resolve() != venv:
        python = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if not python.is_file():
            print('Execute scripts/setup.ps1 para criar o ambiente local.')
            return 1
        from process_control import install_process_job
        install_process_job()
        previous = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            return subprocess.call([str(python), '-B', str(ROOT / 'main.py')], cwd=ROOT)
        finally:
            signal.signal(signal.SIGINT, previous)
    from application import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
