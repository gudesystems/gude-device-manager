"""Browser regression for GitHub #13; requires Playwright and Microsoft Edge."""
import threading
from unittest.mock import Mock

import pytest

playwright = pytest.importorskip('playwright.sync_api')

import upload
from gude.deployDev import DeployDev
from webui import server


@pytest.mark.parametrize('auth', [False, True])
def test_add_device_without_ini(tmp_path, monkeypatch, auth):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server.State, 'results', [])
    monkeypatch.setattr(server.State, 'running', False)
    monkeypatch.setattr(server.State, 'progress', {})
    gbl = Mock()
    gbl.dstMAC = bytes.fromhex('0019320165a8')
    monkeypatch.setattr(upload, 'Gblib', Mock(return_value=gbl))
    monkeypatch.setattr(upload, 'req_get', Mock(side_effect=AssertionError('Internet used')))
    monkeypatch.setattr(DeployDev, 'get_config_filename', Mock(return_value=None))
    mutation = Mock(side_effect=AssertionError('Add Device must only read status'))
    for name in ('update_firmware', 'upload_file', 'reboot', 'factory_reset', 'upload_ssl_certificate'):
        monkeypatch.setattr(DeployDev, name, mutation)
    seen = []
    reject = False

    def status(device, *args, **kwargs):
        credentials = device.get_http_auth()
        seen.append((device.host, credentials.username if credentials else None,
                     credentials.password if credentials else None, device.httpOpts['port']))
        if reject:
            raise ValueError('http request error 401')
        return {'misc': {'prodid': '80xxR2', 'product_name': 'Browser Test Device', 'firm_v': '1.7.0-R2'}}

    monkeypatch.setattr(DeployDev, 'http_get_status_json', status)
    monkeypatch.setattr(DeployDev, 'http_get_config_json', Mock(return_value={'ipv4': {'hostname': 'test'}}))
    httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(error.stack))
            page.on('dialog', lambda dialog: dialog.accept())
            page.goto(f'http://127.0.0.1:{httpd.server_port}')
            playwright.expect(page.locator('#findBtn')).to_be_enabled()
            for attempt in range(2):
                reject = attempt == 1
                page.locator('#addDeviceBtn').click()
                page.locator('#newDevIp').fill('192.0.2.1')
                page.locator('#newDevPort').fill('8080')
                if auth:
                    page.locator('#newDevAuth').check()
                    page.locator('#newDevUser').fill('dialog-user')
                    page.locator('#newDevPass').fill('dialog-secret')
                page.get_by_role('button', name='Add to Current device list', exact=True).click()
                table = page.locator('#devicesTable tbody')
                playwright.expect(table).to_contain_text('401' if reject else 'Browser Test Device')
                playwright.expect(page.locator('#findBtn')).to_be_enabled()
                playwright.expect(table.locator('tr')).to_have_count(1)
                playwright.expect(page.locator('#draftBanner')).to_be_visible()
            assert seen == [('192.0.2.1', 'dialog-user' if auth else None,
                             'dialog-secret' if auth else None, 8080)] * 2
            assert not (tmp_path / 'upload.ini').exists()
            assert not errors, errors
            mutation.assert_not_called()
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
