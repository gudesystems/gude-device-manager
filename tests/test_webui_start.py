import io
from contextlib import redirect_stderr
from unittest.mock import patch

import pytest

import upload
from webui import server


@pytest.mark.parametrize('argv,port', [(['gdm'], 8000), (['gdm', '--webui-port', '8080'], 8080),
                                     (['gdm', '--webui-port=65535'], 65535)])
def test_webui_start(argv, port):
    with patch('sys.argv', argv), patch.object(server, 'serve') as serve, \
            patch.object(upload, 'iterate_list', side_effect=AssertionError('Device operation started')):
        if len(argv) == 1:
            upload.main()
        else:
            with pytest.raises(SystemExit) as stopped:
                upload.main()
            assert stopped.value.code == 0
        serve.assert_called_once_with(host='127.0.0.1', port=port, open_browser=True)


@pytest.mark.parametrize('options', [['--webui-port', '0'], ['--webui-port', '65536'],
                                   ['--webui-port', '-1'], ['--webui-port', 'abc'],
                                   ['--webui-port', '8080', '--forcefw']])
def test_invalid_webui_options(options):
    with patch('sys.argv', ['gdm'] + options), patch.object(server, 'serve') as serve:
        with pytest.raises(SystemExit) as stopped:
            upload.main()
        assert stopped.value.code == 2
        serve.assert_not_called()


def test_webui_bind_error():
    error = io.StringIO()
    with patch.object(server, 'serve', side_effect=OSError('WinError 10013: access denied')), \
            redirect_stderr(error), pytest.raises(SystemExit) as stopped:
        upload.start_webui(8080)
    assert stopped.value.code == 1
    assert '127.0.0.1:8080' in error.getvalue()
    assert '10013' in error.getvalue()
    assert '--webui-port' in error.getvalue()


def test_browser_uses_selected_port():
    with patch.object(server, 'ThreadingHTTPServer') as httpd, \
            patch.object(server.UiSessionMonitor, 'configure'), \
            patch.object(server.UiSessionMonitor, 'stop'), \
            patch.object(server.webbrowser, 'open_new_tab') as browser:
        server.serve(host='127.0.0.1', port=8080, open_browser=True)
        httpd.assert_called_once_with(('127.0.0.1', 8080), server.Handler)
        browser.assert_called_once_with('http://localhost:8080')
