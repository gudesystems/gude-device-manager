"""Exercise logging in fresh processes, including device and WebUI loggers."""
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode,levels', [
    ('default', ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    ('quiet', ('WARNING', 'ERROR', 'CRITICAL')),
    ('debug', ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
])
def test_log_levels_across_modules(mode, levels):
    script = f'''
import logging
import upload
upload.log_config({mode == 'debug'}, {mode == 'quiet'})
upload.log_config({mode == 'debug'}, {mode == 'quiet'})
for name in ('upload', 'gude.deployDev', 'gude.httpDevice', 'webui'):
    for level in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        logging.getLogger(name).log(getattr(logging, level), name + ':' + level)
print('completed')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'completed'
    for name in ('upload', 'gude.deployDev', 'gude.httpDevice', 'webui'):
        for level in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
            assert result.stderr.count(name + ':' + level) == int(level in levels)


def test_conflicting_flags_fail_before_processing():
    result = subprocess.run([sys.executable, 'upload.py', '--quiet', '--debug'],
                            cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert 'not allowed with argument' in result.stderr


def test_webui_start_configures_logging():
    script = '''
import logging
import runpy
import sys
from unittest.mock import patch
sys.argv = ['upload.py']
def serve(**kwargs):
    logging.getLogger('webui').info('webui-started')
with patch('webui.server.serve', side_effect=serve):
    runpy.run_path('upload.py', run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=ROOT,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count('webui-started') == 1
