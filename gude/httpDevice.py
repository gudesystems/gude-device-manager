import json
import time
import requests
from requests.auth import HTTPBasicAuth

from gude.deviceValues import DeviceValues

from gude.gblib import print_progress_bar

import logging
# logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)  # custom logger name can be set


class HttpDevice(DeviceValues):

    def get_log_label(self):
        use_ssl = bool(self.httpOpts.get("ssl"))
        default_port = 443 if use_ssl else 80
        port = self.httpOpts.get("port", default_port)
        endpoint = f"{self.host}:{port}" if port != default_port else self.host
        job_id = getattr(self, "log_job_id", None)
        return f"{job_id} {endpoint}" if job_id else endpoint

    def get_cgi_cmd_param_defaults(self, cmd, extra_params=None):
        if cmd in self.CGI_CMD_JSON_MAP:
            params = dict(self.CGI_CMD_JSON_MAP[cmd])
            params.update({"cmd": cmd})
            if extra_params is not None:
                params.update(extra_params)
            return params
        else:
            raise ValueError("missing cgi cmd params for cmd {0}".format(cmd))

    @staticmethod
    def json_dump(json_data):
        return json.dumps(json_data, sort_keys=True, indent=4, separators=(',', ': '))

    # setHttpPort(443, ssl=True)
    # setHttpPort(80)
    def set_http_port(self, port=80, ssl=False):
        self.httpOpts["ssl"] = ssl
        if port is None:
            if ssl:
                port = 443
            else:
                port = 80
        self.httpOpts["port"] = port

    # setBasicAuth(True, "admin", "admin")
    # setBasicAuth(False, "", "")
    def set_basic_auth(self, basicauth=False, username='', password=''):
        self.httpOpts["basicauth"] = basicauth
        self.httpOpts["username"] = username
        self.httpOpts["password"] = password

    def set_last_req(self, url, code, time_val, size, method="GET"):
        self.lastGetReq["time"] = time_val
        self.lastGetReq["url"] = url
        self.lastGetReq["code"] = code
        log.debug("HTTP {0} {1} {2} {3}".format(method, code, url, size))

    def get_http_url(self, file):
        if self.httpOpts["ssl"]:
            protocol = "https"
            port = (':' + str(self.httpOpts["port"])) if (self.httpOpts["port"] != 443) else ''
        else:
            protocol = "http"
            port = (':' + str(self.httpOpts["port"])) if (self.httpOpts["port"] != 80) else ''

        return '{0}://{1}{2}/{3}'.format(protocol, self.host, port, file)

    def get_http_auth(self):
        auth = None
        if self.httpOpts["basicauth"]:
            auth = HTTPBasicAuth(self.httpOpts["username"], self.httpOpts["password"])
        return auth

    def set_http_timeout(self, timeout):
        self.httpTimeout = timeout

    def set_http_retries(self, retries):
        self.httpRetries = retries

    def set_req_headers(self, req_headers=None):
        self.req_headers = req_headers

    def set_log_job_id(self, job_id=None):
        self.log_job_id = job_id

    def http_get(self, filename, cgi_get_params=None, req_headers=None):
        url = self.get_http_url(filename)
        auth = self.get_http_auth()

        if cgi_get_params is None:
            cgi_get_params = {}

        log.debug(cgi_get_params)

        start = stop = None
        retries = 0
        r = None
        while retries < (1+self.httpRetries) and stop is None:
            try:
                start = time.time()
                if self.httpAutoAddAjaxTimestamp:
                    cgi_get_params["_"] = int(time.time())
                # headers={'Connection': 'close'} DOES NOT WORK WITH PORT FORWARDING
                r = requests.get(url, params=cgi_get_params, verify=False, timeout=self.httpTimeout, auth=auth, headers=req_headers)
                stop = time.time()
            except requests.exceptions.Timeout:
                log.info(f"Timeout {url} {cgi_get_params} {retries}")
                retries += 1

        if r is not None:
            if r.status_code == 200:
                self.set_last_req(r.url, r.status_code, (stop - start), len(r.text))
                return r.text
            elif r.status_code == 401:
                raise requests.exceptions.HTTPError("401 Unauthorized", response=r)
            else:
                self.set_last_req(r.url, r.status_code, (stop - start), None)
                raise ValueError("http request error {0}".format(r.status_code))
        elif stop is None:
            raise requests.exceptions.Timeout("Timeout", response=r)
            
        raise ValueError("http request failed")

    def http_ping(self, timeout=1.0, req_headers=None):
        url = self.get_http_url('index.html')
        auth = self.get_http_auth()
        try:
            r = requests.get(url, params={}, verify=False, timeout=timeout, auth=auth, headers=req_headers)
            return r.status_code == 200
        except (ValueError, Exception):
            return False

    def upload_file(self, file_data, upload_type, cgi_get_params=None, ret_ressource='fwupdate.txt', timeout=10.0, req_headers=None):
        url = self.get_http_url(ret_ressource)
        auth = self.get_http_auth()

        if cgi_get_params is None:
            cgi_get_params = {}

        cgi_get_params["type"] = upload_type
        files = {'fwupload': file_data}

        start = time.time()
        r = requests.post(url, params=cgi_get_params, files=files, verify=False, timeout=timeout, auth=auth, headers=req_headers)
        stop = time.time()

        if r.status_code == 200:
            self.set_last_req(r.url, r.status_code, (stop - start), len(r.text), method="POST")
            return r.text
        else:
            self.set_last_req(r.url, r.status_code, (stop - start), None, method="POST")
            raise ValueError("http request error {0}".format(r.status_code))

    def upload_config(self, file_data):
        log.debug(file_data)
        return self.upload_file(file_data, self.CGI_UPLOAD_TYPE_CONFIG)

    def http_get_json(self, file, cgi_get_params=None, req_headers=None):
        if cgi_get_params is None:
            cgi_get_params = {}

        json_string = self.http_get(file, cgi_get_params, req_headers)
        if json_string:
            return json.loads(json_string)
        else:
            return None

    def http_get_status_json(self, components, cgi_get_params=None, req_headers=None):
        if cgi_get_params is None:
            cgi_get_params = {}
        cgi_get_params["components"] = components
        return self.http_get_json("status.json", cgi_get_params, req_headers)

    def http_get_config_json(self, components, cgi_get_params=None, req_headers=None):
        if cgi_get_params is None:
            cgi_get_params = {}
        cgi_get_params["components"] = components
        return self.http_get_json("config.json", cgi_get_params, req_headers)

    def http_cgi_json_cmd(self, cmd, params=None, merge_defaults=True, req_headers=None):
        if params is None:
            params = {}

        if cmd not in self.CGI_CMD_JSON_MAP:
            json_map = {'resource': {'file': 'status.json', 'components': 0}, 'params': {}}
        else:
            json_map = self.CGI_CMD_JSON_MAP[cmd]

        my_params = {}
        if merge_defaults:
            my_params.update(json_map['params'])

        my_params["cmd"] = cmd
        my_params["components"] = json_map['resource']['components']
        my_params.update(params)

        return self.http_get_json(json_map['resource']['file'], my_params, req_headers)

    def get_all_json(self, req_headers=None):
        self.allConfigJson = self.http_get_config_json(self.JSON_ALL, req_headers)
        self.allStatusJson = self.http_get_status_json(self.JSON_ALL, req_headers)

    def get_eprom_json(self, remove_volatiles=True, req_headers=None):
        self.entities = self.http_get_json('eprom.json', req_headers)

        if not self.entities:
            return False

        if remove_volatiles:
            new_json = []
            for entity in self.entities:
                #
                # completely remove entities
                #   14: CONFIG_ID_BOOT_STATS
                #   30: CONFIG_ID_GSM_COUNTERS
                #   32: CONFIG_ID_GSM_LOGENTRY
                #   41: CONFIG_ID_ENERGY_COUNT (obsolete)
                #   54: CONFIG_ID_EXT_ENERGY_COUNT
                #
                if entity["id"] not in [14, 30, 32, 41, 54]:
                    new_json.append(entity)

                #
                # whiteout entities parts
                #
                if entity["id"] == 36:
                    # watchdog status
                    entity["data"] = entity["data"][:2] + "00" + entity["data"][5:]

                if entity["id"] == 38:
                    # whiteout current port state (remember last state maybe is fab default)
                    opt_byte = int(bytearray.fromhex(entity["data"][:2])[0]) & 0xFE
                    entity["data"] = f'{opt_byte:0>2X}' + entity["data"][2:]

            self.entities = new_json
        return self.entities

    def flush_config_buffer(self):
        self.allConfigJson = None
        self.allStatusJson = None
        self.entities = None

    def wait_reboot(self, max_wait_secs=20.0, pre_wait_secs=5.0, req_headers=None, show_progress_bar=True, progress_cb=None):
        total = int(pre_wait_secs) + int(max_wait_secs)
        for i in range(0, int(pre_wait_secs)):
            # log.info(".")
            
            if progress_cb:
                pct = (100 / total) * i if total > 0 else 0
                progress_cb({"ip": self.host, "type": "progress", "progress": pct, "status": f"Rebooting {i}/{total}s"})

            if show_progress_bar:
                print_progress_bar(i, total, fill='#', clear=' ', unit='seconds')
            else:
                if i % 5 == 0:
                    log.info(f"[{self.get_log_label()}] Rebooting... {i}/{total} seconds")
            time.sleep(1)
        retries = max_wait_secs
        while retries:
            if self.http_ping(1.0, req_headers):
                if show_progress_bar:
                    print_progress_bar(total, total, fill='#', clear=' ', unit='seconds',
                                       actual=int(pre_wait_secs) + max_wait_secs - retries)
                log.info(f"[{self.get_log_label()}] Device reachable")
                time.sleep(1)
                return True
            else:
                retries -= 1
                # log.info(".")
                current_sec = int(pre_wait_secs) + max_wait_secs - retries
                
                if progress_cb:
                    pct = (100 / total) * current_sec if total > 0 else 0
                    progress_cb({"ip": self.host, "type": "progress", "progress": pct, "status": f"Rebooting {current_sec}/{total}s"})

                if show_progress_bar:
                    print_progress_bar(current_sec, total, fill='#', clear=' ',
                                       unit='seconds')
                else:
                    if current_sec % 5 == 0:
                        log.info(f"[{self.get_log_label()}] Rebooting... {current_sec}/{total} seconds")

        log.error(f"[{self.get_log_label()}] ERROR: no reply, giving up")
        return False

    def reboot_cmd(self, cmd, wait_reboot=False, max_wait_secs=20.0, req_headers=None, show_progress_bar=True, progress_cb=None):
        self.http_cgi_json_cmd(cmd, req_headers)
        if wait_reboot:
            return self.wait_reboot(max_wait_secs, show_progress_bar=show_progress_bar, progress_cb=progress_cb)
        else:
            return True

    #
    # reboot device to Fab Defaults
    #
    def reboot_fab(self, wait_reboot=True, show_progress_bar=True, progress_cb=None):
        log.info(f"[{self.get_log_label()}] Reboot to FabSettings...")
        self.flush_config_buffer()
        ret = self.reboot_cmd(self.CGI_CMD_RESET_TO_FAB, wait_reboot, show_progress_bar=show_progress_bar, progress_cb=progress_cb)
        return ret

    #
    # reboot device
    #
    def reboot(self, wait_reboot=True, max_wait_secs=20, show_progress_bar=True, progress_cb=None):
        log.info(f"[{self.get_log_label()}] Rebooting...")
        self.flush_config_buffer()
        ret = self.reboot_cmd(self.CGI_CMD_RESET, wait_reboot, max_wait_secs, show_progress_bar=show_progress_bar, progress_cb=progress_cb)
        return ret

    #
    # switch port
    #
    def http_switch_port(self, port, status, req_headers=None):
        json_data = self.http_cgi_json_cmd(self.CGI_CMD_SWITCH_POWERPORTS, {"p": port, "s": status}, req_headers)
        if json_data['outputs'][port-1]['state'] != status:
            raise ValueError("illegal switch state")
        return json_data

    def set_bank_source(self, bank_id, source, req_headers=None):
        json_data = self.http_cgi_json_cmd(self.CGI_CMD_CONFIG_POWERPORTS,
                                           {f'banksource[{bank_id}]': '.'.join((str(x) for x in source))}, req_headers)
        return json_data

    def http_switch_ets_power_source(self, source, req_headers=None):
        json_data = self.http_cgi_json_cmd(self.CGI_CMD_SWITCH_ETS, {"switch": source}, req_headers)
        return json_data['hardware']['power'][0]['outputs'][0]['source']['connected']

    #
    # cancel Batchmode
    #
    def http_cancel_batch_mode(self, port, req_headers=None):
        return self.http_cgi_json_cmd(self.CGI_CMD_CANCEL_BATCHMODES, {"p": port}, req_headers)

    #
    # config Clock (helper):
    #
    def http_config_clock(self, ntp_enabled, ntp_srv_1, ntp_srv_2, tz_offset, dst_enabled, req_headers=None):
        cgi = {
           "ntp": ntp_enabled,
           "ntpsrv1": ntp_srv_1,
           "ntpsrv2": ntp_srv_2,
           "tz_offset": tz_offset,
           "dst": dst_enabled
        }
        return self.http_cgi_json_cmd(self.CGI_CMD_CONFIG_CLOCK, cgi, req_headers)

    def trigger_event_message(self, typ, idx=0, context=0, pipe=DeviceValues.EVENT_MSG_PIPES_ETH, extra=None, req_headers=None):
        cgi = {'id': typ, 'idx': idx, 'context': context, 'pipe': pipe}
        if extra is not None:
            cgi['extra'] = extra
        log.info(f"[{self.get_log_label()}] Trigger test event {typ}.{idx} context({context}) pipe({pipe}) extra({extra})")
        return self.http_cgi_json_cmd(HttpDevice.CGI_CMD_TEST_EVENT_MSG, cgi, req_headers)

    def export_config(self, req_headers=None):
        return self.http_get("config.txt", req_headers)

    def get_ram_usage(self, req_headers=None):
        debug_json = self.http_get_json('debug.json', req_headers)
        return debug_json["internal"]["free_mem"], debug_json["external"]["free_mem"]

    def get_mem_log(self, facility=None, max_level=None, minid=None, req_headers=None):
        cgi = {}
        if facility is not None:
            cgi['facility'] = facility
        if max_level is not None:
            cgi['maxlevel'] = max_level
        if minid is not None:
            cgi['minid'] = minid

        return self.http_get_json('memlog.json', cgi, req_headers)

    def get_mem_log_last_id(self, facility=None, max_level=None):
        last_id = 0
        _log = self.get_mem_log(facility=facility, max_level=max_level)
        if len(_log):
            last_id = _log[-1]['id']
        return last_id

    @staticmethod
    def hyst_by_prec(precision):
        hyst = 1
        if precision == 1:
            hyst = 0.5
        if precision == 2:
            hyst = 0.05
        if precision == 3:
            hyst = 0.005
        if precision == 4:
            hyst = 0.0005

        return hyst

    def __init__(self, host, req_headers=None):
        self.host = host
        self.log_job_id = None
        self.httpOpts = {
            "ssl": False,
            "port": 80,
            "basicauth": False,
            "sessionauth": False,
            "username": "admin",
            "password": "admin",
        }
        self.lastGetReq = {
            "time": None,
            "url": None,
            "code": None,
        }
        self.req_headers = req_headers
        self.allConfigJson = None
        self.allStatusJson = None
        self.entities = None
