import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from requests.exceptions import HTTPError, Timeout

import upload
from gude.deployDev import DeployDev
from webui import server


CATALOG = [{
    'model': '2111', 'rev': '', 'subpath': 'gude',
    'json': 'firmware-enc2111.json', 'filename': 'firmware-enc2111_v{version}.bin',
    'type': 'enc', 'version': '1.7.1', 'date': '14.01.2025',
    'size': '1.1 MB', 'version_list': ['1.7.1', '1.7.0'],
}]


class OfflineDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / 'fw' / 'online-firmware.ini'
        self.patch = patch.object(upload, 'FIRMWARE_CACHE', self.cache)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def save_catalog(self):
        response = Mock()
        response.json.return_value = CATALOG
        with patch.object(upload, 'req_get', return_value=response):
            return upload.refresh_fw_infos()

    def handler(self):
        handler = object.__new__(server.Handler)
        handler.wfile = io.BytesIO()
        handler._send = Mock()
        return handler

    def test_catalog_survives_reload_without_network(self):
        self.save_catalog()
        with patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')):
            restored = upload.load_cached_fw_infos()
        self.assertEqual(restored['2111']['version'], '1.7.1')
        self.assertEqual(restored['2111']['version_list'], '1.7.1, 1.7.0')

    def test_failed_refresh_preserves_previous_catalog(self):
        self.save_catalog()
        original = self.cache.read_bytes()
        for failure in (Timeout('offline'), HTTPError('503'), ValueError('invalid JSON')):
            with self.subTest(failure=failure):
                with patch.object(upload, 'req_get', side_effect=failure):
                    with self.assertRaises(type(failure)):
                        upload.refresh_fw_infos()
                self.assertEqual(self.cache.read_bytes(), original)

    def test_invalid_catalog_does_not_replace_saved_data(self):
        self.save_catalog()
        original = self.cache.read_bytes()
        for payload in ({'error': 'unavailable'}, [], [{}]):
            response = Mock()
            response.json.return_value = payload
            with patch.object(upload, 'req_get', return_value=response):
                with self.assertRaises((ValueError, KeyError)):
                    upload.refresh_fw_infos()
            self.assertEqual(self.cache.read_bytes(), original)

    def test_http_error_checked_before_decoding(self):
        response = Mock()
        response.raise_for_status.side_effect = HTTPError('503')
        with patch.object(upload, 'req_get', return_value=response):
            with self.assertRaises(HTTPError):
                upload.refresh_fw_infos()
        response.json.assert_not_called()

    def test_write_failure_preserves_previous_catalog(self):
        self.save_catalog()
        original = self.cache.read_bytes()
        response = Mock()
        response.json.return_value = CATALOG
        with patch.object(upload, 'req_get', return_value=response), \
                patch.object(upload.os, 'replace', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                upload.refresh_fw_infos()
        self.assertEqual(self.cache.read_bytes(), original)
        self.assertEqual(list(self.cache.parent.iterdir()), [self.cache])

    def test_missing_and_corrupt_cache_allow_discovery(self):
        for contents in (None, b'not an ini file', b'\xff\xfe'):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.cache.parent.mkdir(exist_ok=True)
                    self.cache.write_bytes(contents)
                with patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')), \
                        patch.object(upload, 'generate_ip_list', return_value=['192.0.2.1']) as discover, \
                        patch.object(upload, 'iterate_list', return_value=['device']) as process:
                    results = upload.run_processing_from_options(
                        upload_ini=str(Path(self.directory.name) / 'absent.ini'),
                        gbl=True, status=True, onlineupdate=True)
                discover.assert_called_once()
                self.assertEqual(results, ['device'])
                self.assertEqual(process.call_args.args[1].sections(), [])

    def test_web_search_refresh_and_no_update_use_saved_metadata(self):
        self.save_catalog()
        with patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')), \
                patch.object(upload, 'generate_ip_list', return_value=['192.0.2.1']), \
                patch.object(upload, 'iterate_list', return_value=[]) as process:
            server._run_gbl_query_async()
            server._run_status_selected_async(['192.0.2.1'])
            server._run_update_selected_async(
                ['192.0.2.1'], custom_firmware={'192.0.2.1': '__no_update__'})
        self.assertEqual(process.call_count, 3)
        for call in process.call_args_list:
            self.assertEqual(call.args[1]['2111']['version'], '1.7.1')
        self.assertFalse(server.State.running)

    def test_refresh_api_failure_is_clear_and_local_file_listing_still_works(self):
        self.save_catalog()
        handler = self.handler()
        with patch.object(upload, 'req_get', side_effect=Timeout('offline')):
            handler._api_refresh_firmware_info()
        self.assertEqual(handler._send.call_args.args[0], 502)
        payload = json.loads(handler.wfile.getvalue())
        self.assertFalse(payload['ok'])
        self.assertIn('device discovery is still available', payload['error'])
        handler = self.handler()
        with patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')):
            handler._api_firmware()
        self.assertEqual(handler._send.call_args.args[0], 200)
        self.assertTrue(json.loads(handler.wfile.getvalue())['last_update'])

    def test_refresh_api_saves_catalog(self):
        handler = self.handler()
        response = Mock()
        response.json.return_value = CATALOG
        with patch.object(upload, 'req_get', return_value=response):
            handler._api_refresh_firmware_info()
        self.assertEqual(handler._send.call_args.args[0], 200)
        self.assertTrue(json.loads(handler.wfile.getvalue())['ok'])
        self.assertEqual(upload.load_cached_fw_infos()['2111']['version'], '1.7.1')

    def test_cli_refresh_does_not_contact_devices(self):
        with patch('sys.argv', ['upload.py', '--refresh-firmware-info']), \
                patch.object(upload, 'refresh_fw_infos') as refresh, \
                patch.object(upload, 'generate_ip_list') as discover:
            with self.assertRaises(SystemExit) as result:
                upload.main()
        self.assertEqual(result.exception.code, 0)
        refresh.assert_called_once()
        discover.assert_not_called()

    def test_current_firmware_needs_no_online_lookup(self):
        cfg = self.save_catalog()
        device = DeployDev('192.0.2.1')
        with patch('gude.deployDev.requests.get', side_effect=AssertionError('Internet used')):
            result = device.update_firmware(
                {'prodid': '2111', 'firm_v': '1.7.1'}, cfg, online_update=True)
        self.assertFalse(result['updated'])

    def test_local_binary_update_uses_cached_version_without_internet(self):
        cfg = self.save_catalog()
        binary = Path(self.directory.name) / 'firmware-enc2111_v1.7.1.bin'
        binary.write_bytes(b'test firmware')
        device = DeployDev('192.0.2.1')
        uploaded = []

        def fake_upload():
            uploaded.append(device.fw)
            device.fw = None

        with patch('gude.deployDev.requests.get', side_effect=AssertionError('Internet used')), \
                patch('gude.deployDev.time.sleep'), \
                patch.object(device, 'threaded_upload', side_effect=fake_upload), \
                patch.object(device, 'reboot', return_value=True), \
                patch.object(device, 'http_get_status_json', side_effect=[
                    {'fileupload': {}}, {'misc': {'firm_v': '1.7.1'}}]):
            result = device.update_firmware(
                {'prodid': '2111', 'firm_v': '1.7.0'}, cfg,
                fw_dir=self.directory.name, online_update=True)
        self.assertTrue(result['updated'])
        self.assertEqual(uploaded, [b'test firmware'])

    def test_cli_status_uses_cache_without_internet(self):
        self.save_catalog()
        with patch('sys.argv', ['upload.py', '--status', '--onlineupdate', '--gbl']), \
                patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')):
            args, config, firmware, my_ip = upload.parse_args()
        self.assertTrue(args.status)
        self.assertEqual(firmware['2111']['version'], '1.7.1')

    def test_uploaded_files_reach_the_selected_devices_without_any_catalog(self):
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(self.directory.name)
        files = {
            '192.0.2.1': 'firmware-epc8041-r2_v1.7.0.bin',
            '192.0.2.2': 'firmware-epc8041-r2_v1.6.0.bin',
        }
        for filename in files.values():
            handler = self.handler()
            content = filename.encode()
            handler.headers = {'X-Filename': filename, 'Content-Length': str(len(content))}
            handler.rfile = io.BytesIO(content)
            handler._api_upload_firmware()
            self.assertEqual(handler._send.call_args.args[0], 200)
            self.assertEqual((Path('fw') / filename).read_bytes(), content)

        listing = self.handler()
        listing._api_firmware()
        self.assertEqual({f['name'] for f in json.loads(listing.wfile.getvalue())['files']},
                         set(files.values()))
        self.assertFalse(self.cache.exists())
        applied = {}

        def update(device, data, firmware, fw_dir, **kwargs):
            filename = firmware.get(data['prodid'], 'filename')
            applied[device.host] = (filename, (Path(fw_dir) / filename).read_bytes())
            return {'updated': True, 'final_version': '1.7.0-R2', 'status_message': 'updated'}

        gbl = Mock()
        gbl.dstMAC = bytes.fromhex('0019320165a8')
        with patch.object(upload, 'Gblib', return_value=gbl), \
                patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')), \
                patch.object(DeployDev, 'get_config_filename', return_value=None), \
                patch.object(DeployDev, 'http_get_status_json', side_effect=lambda *a, **k: {
                    'misc': {'prodid': '80xxR2', 'product_name': 'Expert Power Control 8041-1',
                             'firm_v': '1.6.0-R2'}}), \
                patch.object(DeployDev, 'http_get_config_json', return_value={'ipv4': {'hostname': 'test'}}), \
                patch.object(DeployDev, 'update_firmware', autospec=True, side_effect=update):
            server._run_update_selected_async(
                list(files) + ['192.0.2.3'],
                custom_firmware=dict(files, **{'192.0.2.3': '__no_update__'}))
        self.assertEqual(applied, {ip: (filename, filename.encode()) for ip, filename in files.items()})
        self.assertEqual(len(server.State.results), 3)
        self.assertTrue(all(r.success for r in server.State.results))
        skipped = next(r for r in server.State.results if r.ip == '192.0.2.3')
        self.assertEqual(skipped.firmware_status, 'Skipped (No Update)')

    def test_selected_actions_keep_auth_without_adding_stored_hosts(self):
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(self.directory.name)
        Path('upload.ini').write_text(
            '[hosts]\nip1 = 192.0.2.99\ngbl = search\n'
            '[httpDefaults]\nauth = 1\nusername = global-user\npassword = global-secret\n'
            '[192.0.2.2]\nauth = 1\nusername = device-user\npassword = device-secret\n'
            '[192.0.2.3]\nauth = 0\n')
        hosts = ['192.0.2.1:8080', '192.0.2.2', '192.0.2.3']
        seen = {}

        def status(device, *args, **kwargs):
            auth = device.get_http_auth()
            seen[device.host] = (auth.username, auth.password) if auth else None
            return {'misc': {'prodid': '80xxR2', 'product_name': 'Test Device', 'firm_v': '1.7.0-R2'}}

        gbl = Mock()
        gbl.dstMAC = bytes.fromhex('0019320165a8')
        gbl.recv_bc.side_effect = AssertionError('Unselected broadcast was included')
        with patch.object(upload, 'Gblib', return_value=gbl) as gbl_class, \
                patch.object(upload, 'req_get', side_effect=AssertionError('Internet used')), \
                patch.object(DeployDev, 'get_config_filename', return_value=None), \
                patch.object(DeployDev, 'http_get_status_json', autospec=True, side_effect=status), \
                patch.object(DeployDev, 'http_get_config_json', return_value={'ipv4': {'hostname': 'test'}}):
            gbl_class.recv_bc.side_effect = AssertionError('Unselected broadcast was included')
            for operation in ('status', 'update'):
                seen.clear()
                if operation == 'status':
                    server._run_status_selected_async(hosts)
                else:
                    server._run_update_selected_async(hosts, custom_firmware={h: '__no_update__' for h in hosts})
                self.assertEqual(seen, {
                    '192.0.2.1': ('global-user', 'global-secret'),
                    '192.0.2.2': ('device-user', 'device-secret'),
                    '192.0.2.3': None,
                })

    def test_force_upload_http_rejection_aborts_before_reboot(self):
        cfg = self.save_catalog()
        (Path(self.directory.name) / 'firmware-enc2111_v1.7.1.bin').write_bytes(b'test firmware')
        for code in (401, 403):
            with self.subTest(code=code):
                device = DeployDev('192.0.2.1')
                device.set_basic_auth(True, 'test-user', 'test-password')
                response = Mock(status_code=code, url='http://192.0.2.1/fwupdate.txt')
                with patch('gude.httpDevice.requests.post', return_value=response) as post, \
                        patch.object(device, 'http_get_status_json') as status, \
                        patch.object(device, 'reboot') as reboot:
                    with self.assertRaisesRegex(ValueError, f'http request error {code}'):
                        device.update_firmware(
                            {'prodid': '2111', 'firm_v': '1.7.1'}, cfg,
                            fw_dir=self.directory.name, forced=True, online_update=True)
                self.assertEqual(post.call_args.kwargs['auth'].username, 'test-user')
                self.assertEqual(post.call_args.kwargs['auth'].password, 'test-password')
                reboot.assert_not_called()
                status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
