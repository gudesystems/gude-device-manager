#!/usr/bin/env python3

from configparser import ConfigParser
from datetime import date
from argparse import ArgumentParser, Namespace, RawDescriptionHelpFormatter
import os
import sys
import ipaddress
from socket import gaierror
from requests import get as req_get
from requests.exceptions import Timeout, HTTPError, RequestException
from typing import Tuple, Optional, List, Dict, Any, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from gude.deployDev import DeployDev
from gude.firmware_target import (
    format_firmware_version_for_display,
    is_explicit_firmware_selection,
    resolve_configured_firmware_version,
)
from gude.gblib import Gblib
import json
import re

from gude import file_search

import logging


BASE_URL = "https://files.gude-systems.com/fw"


log = logging.getLogger(__name__)


def log_config(debug: bool, quiet: bool) -> None:
    """Configure console logging for both the CLI and application modules."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, '_gude_console', False):
            root.removeHandler(existing)
            existing.close()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler._gude_console = True
    formatter = logging.Formatter(
        fmt='%(asctime)s %(name)-18s %(levelname)-8s %(message)s')
    handler.setFormatter(formatter)
    level = logging.DEBUG if debug else logging.WARNING if quiet else logging.INFO
    handler.setLevel(level)
    root.addHandler(handler)
    root.setLevel(level)


def fetch_latest_fw_infos(base_url: str   = BASE_URL) -> ConfigParser:
    """
    Return a ready-to-use ConfigParser whose layout matches

        [url]
        basepath = https://files.gude-systems.com/fw

        [enc2111]
        subpath      = gude
        json         = firmware-enc2111.json
        version      = 1.7.1
        filename     = firmware-enc2111_v{version}.bin
        date         = 14.01.2025
        size         = 1.1 MB
        version_list = 1.7.1, 1.7.0, 1.6.1, …

    The `version_list` is stored as a single comma-separated string, because
    the INI format has no native list type.
    """
    # --- download & decode --------------------------------------------------
    log.debug(f"Reading {base_url} ...")
    payload: List[Dict[str, Any]] = req_get(base_url, timeout=30).json()

    # --- build config -------------------------------------------------------
    cfg = ConfigParser()
    cfg["url"] = {"basepath": base_url, "last_update": date.today().isoformat()}

    for entry in payload:
        if entry["rev"] and entry["rev"] == 2:
            section = f"{entry['model']}R{entry['rev']}"  # e.g. "8031R2"
        else:
            section = f"{entry['model']}"  # e.g. "2111"
        cfg[section] = {
            "subpath":  entry["subpath"],
            "json":     entry["json"],
            "filename": entry["filename"],
            "type":     entry["type"],
            "model":    entry["model"],
            "rev":      entry["rev"],
            "version":  entry["version"],
            "date":     entry["date"],
            "size":     entry["size"],
            # keep the list – flattened into one string
            "version_list": ", ".join(entry["version_list"]),
        }

    return cfg


# --- Firmware/model resolution helpers --------------------------------------

def _parse_specific_model_from_product_name(product_name: str) -> Optional[str]:
    """
    Try to derive a specific 6-digit EPC model from a product name.
    Example: "Expert Power Control 87-1230-18" -> "871230".
    Returns None if no pattern match.
    """
    try:
        m = re.search(r"\b87-([0-9]{4})\b", product_name)
        if not m:
            return None
        digits = m.group(1)
        return f"87{digits}"
    except Exception:
        return None


def _extract_model_candidates_from_product_name(product_name: str, rev_suffix: str = "") -> List[str]:
    """
    Extract likely model ids from product_name.
    Examples:
    - "Expert Power Control 8045" -> ["8045", "8045R2"] (if rev_suffix="R2")
    - "Expert Power Control 87-1230-18" -> ["871230", "871230R2"] (if rev_suffix="R2")
    """
    cands: List[str] = []
    if not isinstance(product_name, str) or not product_name.strip():
        return cands

    try:
        # Keep existing EPC87 family handling.
        specific_87 = _parse_specific_model_from_product_name(product_name)
        if specific_87:
            cands.append(specific_87)
            if rev_suffix:
                cands.append(f"{specific_87}{rev_suffix}")

        # Generic model tokens: 4-6 consecutive digits often represent model ids.
        for tok in re.findall(r"\b\d{4,6}\b", product_name):
            cands.append(tok)
            if rev_suffix:
                cands.append(f"{tok}{rev_suffix}")
    except Exception:
        return cands

    # Deduplicate while preserving order
    seen = set()
    out: List[str] = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _version_key(v: str) -> tuple:
    """Turn a version like '1.2.3' or '1.2.3-R2' into a sortable key."""
    if not isinstance(v, str):
        return (0,)
    # Strip known suffixes like '-R2' for ordering purposes only
    core = v.split('-')[0]
    parts = re.findall(r"\d+", core)
    try:
        return tuple(int(p) for p in parts)
    except Exception:
        return (0,)


def resolve_prodid(
    *,
    actual_prodid: str,
    product_name: str,
    firmware_cfg: ConfigParser,
    repl_map: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve a device's product id to a concrete firmware section present in firmware_cfg.
    - Handles exact match
    - Applies replacement mapping (repl_map)
    - Handles wildcard prodids with 'x' or 'xx': uses product_name to specialize, then regex match
    Returns (selected_prodid, latest_known_version) or (None, None) if unresolved.
    """
    if not actual_prodid:
        return None, None

    sections = list(firmware_cfg.sections())
    sections_set = set(sections)
    # case-insensitive lookup map for sections
    sec_lc_map: Dict[str, str] = {s.lower(): s for s in sections}

    # Helper: get version from cfg if available
    def get_ver(sec: str) -> Optional[str]:
        try:
            return firmware_cfg.get(sec, 'version', fallback=None)
        except Exception:
            return None

    # Normalize for comparisons but keep original section keys
    ap_l = actual_prodid.strip()

    # 1) Exact match (case-insensitive convenience)
    if ap_l in sections_set:
        return ap_l, get_ver(ap_l)
    if ap_l.lower() in sec_lc_map:
        sec = sec_lc_map[ap_l.lower()]
        return sec, get_ver(sec)

    # 2) Apply replacement map
    if repl_map and ap_l in repl_map:
        cand = repl_map[ap_l]
        if cand in sections_set:
            return cand, get_ver(cand)
        if cand.lower() in sec_lc_map:
            sec = sec_lc_map[cand.lower()]
            return sec, get_ver(sec)

    # 3) Handle wildcard prodids: contains any 'x' or 'X' or 'xx'
    has_wildcard = 'x' in ap_l or 'X' in ap_l
    selected: Optional[str] = None
    selected_ver: Optional[str] = None

    if has_wildcard:
        rev_m = re.search(r"(R\d+)$", ap_l, flags=re.IGNORECASE)
        rev_suffix = rev_m.group(1).upper() if rev_m else ""

        # 3a) Try to specialize via product_name (e.g. 871x10 -> 871230)
        specific = _parse_specific_model_from_product_name(product_name or '')
        if specific and specific in sections_set:
            selected = specific
            selected_ver = get_ver(specific)
        else:
            # 3b) Regex-expand the wildcard and find best candidate by version
            # Build case-sensitive pattern replacing 'xx' -> '\d\d', 'x' -> '\d'
            pat_str = re.escape(ap_l)
            pat_str = pat_str.replace('XX', r'\d\d').replace('xx', r'\d\d')
            pat_str = pat_str.replace('X', r'\d').replace('x', r'\d')
            pat = re.compile(f"^{pat_str}$")
            matches = [sec for sec in sections if pat.match(sec)]
            if matches:
                # Prefer concrete model parsed from product_name when available.
                for cand in _extract_model_candidates_from_product_name(product_name or '', rev_suffix=rev_suffix):
                    sec = sec_lc_map.get(cand.lower())
                    if sec and sec in matches:
                        selected = sec
                        selected_ver = get_ver(sec)
                        break
                if selected:
                    return selected, selected_ver

                # Prefer the one with highest version number if available
                best = None
                best_ver_key = None
                best_ver_str = None
                # Sort for deterministic tie-breaking across runs.
                for sec in sorted(matches):
                    ver = get_ver(sec)
                    key = _version_key(ver) if ver else (0,)
                    if best is None or key > best_ver_key:  # type: ignore
                        best = sec
                        best_ver_key = key
                        best_ver_str = ver
                selected = best
                selected_ver = best_ver_str

        # 3c) If nothing found but a family alias section exists (e.g., '871x10' in version.ini), use it
        if not selected:
            if ap_l in sections_set:
                selected = ap_l
                selected_ver = get_ver(ap_l)
            elif ap_l.lower() in sec_lc_map:
                sec = sec_lc_map[ap_l.lower()]
                selected = sec
                selected_ver = get_ver(sec)

    # 4) Return what we have (may still be None)
    return selected, selected_ver


def find_offline_compatible_prodid(
    *,
    selected_prodid: str,
    firmware_cfg: ConfigParser,
    fw_dir_default: str,
) -> Optional[str]:
    """
    Offline fallback for shared firmware families.
    If selected_prodid's local firmware file is missing, try compatible sections
    in the same 2-digit family and same revision suffix (e.g. 80xxR2).
    Returns replacement section name or None.
    """
    if not selected_prodid or not firmware_cfg.has_section(selected_prodid):
        return None

    m = re.match(r"^(\d{2})\d{2}(R\d+)$", selected_prodid, flags=re.IGNORECASE)
    if not m:
        return None

    fam_prefix = m.group(1)
    rev_suffix = m.group(2).upper()
    fam_pat = re.compile(rf"^{re.escape(fam_prefix)}\d{{2}}{re.escape(rev_suffix)}$", flags=re.IGNORECASE)

    def local_file_exists(sec: str) -> bool:
        try:
            if not firmware_cfg.has_section(sec):
                return False
            version = firmware_cfg.get(sec, 'version', fallback='').strip()
            filename_tpl = firmware_cfg.get(sec, 'filename', fallback='').strip()
            if not version or not filename_tpl:
                return False
            filename = filename_tpl.replace('{version}', version)
            fw_dir = firmware_cfg.get(sec, 'path', fallback=fw_dir_default)
            return os.path.isfile(os.path.join(fw_dir, filename))
        except Exception:
            return False

    if local_file_exists(selected_prodid):
        return None

    candidates = [sec for sec in firmware_cfg.sections() if fam_pat.match(sec)]
    for sec in sorted(candidates):
        if sec == selected_prodid:
            continue
        if local_file_exists(sec):
            return sec
    return None


def add_devices_to_config(_args: Namespace, _config: ConfigParser) -> ConfigParser:
    """
    Add devices from command line args to the config parser object

    :param _args: Parsed command line arguments
    :param _config: Configuration parser object
    :return: Modified configuration parser object
    """
    if _args.devices is not None:
        log.debug(f"Adding devices to config: {_args.devices}")
        for section, settings in _args.devices.items():
            set_config_defaults(_config, section, settings, overwrite=True)
    return _config


def set_config_defaults(_config: ConfigParser, section: str, settings: dict, overwrite: bool = False) -> None:
    """
    Function to set default configuration values for a section.

    :param _config: Configuration parser object to be edited
    :param section: Section name to apply settings to
    :param settings: Dictionary of default settings to apply
    :param overwrite: Whether to overwrite existing values (default: False)
    """
    if not _config.has_section(section):
        _config[section] = {}

    for key, value in settings.items():
        if overwrite or not _config.has_option(section, key):
            _config[section][key] = str(value)

# Default settings dictionaries
DEFAULT_SETTINGS = {
    'defaults': {
        'httpTimeout': '3.0',
        'gblTimeout': '1.0',
        'fwdir': 'fw'
    },
    'httpDefaults': {
        'port': '80',
        'ssl': '0',
        'auth': '0',
        'username': '',
        'password': ''
    },
    'hosts': {}
}

def save_device_to_config(section: str, settings: dict, filename: str = 'upload.ini') -> None:
    """
    Save or update a device configuration in the INI file.

    :param section: The section name (e.g. IP address)
    :param settings: Dictionary of settings for the section
    :param filename: The INI file to update
    """
    config = ConfigParser(strict=False)
    # Preserve case of keys? ConfigParser defaults to lowercase keys.
    # To preserve case, we'd need: config.optionxform = str

    if os.path.exists(filename):
        config.read(filename)

    # Ensure hosts section exists and add the device key if it's an IP
    # We follow the pattern: Section [IP] exists -> add IP to [hosts] as ipX=IP
    if 'hosts' not in config:
        config['hosts'] = {}

    # Check if section already exists in hosts values to avoid duplication
    # or if we need to add a new ipN key
    known_hosts = set(config['hosts'].values())
    if section not in known_hosts:
        # find next free ipX
        existing_keys = [k for k in config['hosts'].keys() if k.startswith('ip') and k[2:].isdigit()]
        next_idx = 1
        if existing_keys:
            indices = [int(k[2:]) for k in existing_keys]
            next_idx = max(indices) + 1
        config['hosts'][f'ip{next_idx}'] = section

    if section not in config:
        config[section] = {}

    for k, v in settings.items():
        if v is not None:
             config[section][k] = str(v)

    with open(filename, 'w') as f:
        config.write(f)

def merge_ini_file(content: str, target_filename: str = 'upload.ini') -> None:
    """
    Merge content from an uploaded INI file into the target INI file.

    :param content: String content of the uploaded INI
    :param target_filename: The local INI file to merge into
    """
    target = ConfigParser(strict=False)
    if os.path.exists(target_filename):
        target.read(target_filename)

    source = ConfigParser(strict=False)
    source.read_string(content)

    # Merge hosts
    if 'hosts' not in target:
        target['hosts'] = {}

    # existing target host values
    existing_hosts = set(target['hosts'].values())

    # next index for target
    existing_keys = [k for k in target['hosts'].keys() if k.startswith('ip') and k[2:].isdigit()]
    next_idx = 1
    if existing_keys:
        indices = [int(k[2:]) for k in existing_keys]
        next_idx = max(indices) + 1

    if 'hosts' in source:
        for k, v in source['hosts'].items():
            if v not in existing_hosts:
                target['hosts'][f'ip{next_idx}'] = v
                existing_hosts.add(v)
                next_idx += 1

    # Merge other sections
    for section in source.sections():
        if section == 'hosts':
            continue

        if section not in target:
            target[section] = {}

        for k, v in source[section].items():
            target[section][k] = v

    with open(target_filename, 'w') as f:
        target.write(f)

def generate_ini_export(selected_keys: List[str] = None, export_all: bool = False, source_filename: str = 'upload.ini') -> str:
    """
    Generate an INI string for export.

    :param selected_keys: List of section names (IPs) to include
    :param export_all: If True, export all devices found in [hosts]
    :param source_filename: Source INI file
    :return: String content of the INI
    """
    source = ConfigParser(strict=False)
    if os.path.exists(source_filename):
        source.read(source_filename)

    export = ConfigParser(strict=False)
    export['hosts'] = {}

    # Determine which devices to export
    devices_to_export = []

    if export_all and 'hosts' in source:
        # Add all explicitly listed hosts
        for k, v in source['hosts'].items():
            # Skip comments or special keys like gbl if purely exporting devices?
            # Or export everything?
            # Request says "export option to export an upload.ini... (using elements selected...)"
            # User later said "create new ini (based on devices available in webpage)"
            if k.startswith('ip') or k.startswith('net'):
                devices_to_export.append(v)
            elif k == 'gbl':
                 export['hosts']['gbl'] = v
    elif selected_keys:
        devices_to_export = selected_keys

    # Add devices to export [hosts] and copy their sections
    export_idx = 1
    for dev in devices_to_export:
        export['hosts'][f'ip{export_idx}'] = dev
        export_idx += 1

        # Copy section if it exists
        if source.has_section(dev):
            export[dev] = {}
            for k, v in source[dev].items():
                export[dev][k] = v

    # Copy defaults/httpDefaults if they exist, as they are useful context
    for common in ['defaults', 'httpDefaults']:
        if source.has_section(common):
            export[common] = {}
            for k, v in source[common].items():
                export[common][k] = v

    import io
    output = io.StringIO()
    export.write(output)
    return output.getvalue()


def overwrite_ini_hosts(
        hosts_list: List[str],
        host_settings: Optional[Dict[str, Dict[str, Any]]] = None,
        filename: str = 'upload.ini') -> None:
    """
    Overwrite the [hosts] section of the INI file with the provided list of hosts.
    Preserves other sections like [defaults], [httpDefaults], and specific device sections if they exist.
    Optionally updates per-host section settings for hosts present in the provided list.

    :param hosts_list: List of IP strings (or IP:Port) to set as the new hosts list.
    :param host_settings: Optional map of host -> settings dict to write into host sections.
    :param filename: The INI file to update.
    """
    config = ConfigParser(strict=False)
    if os.path.exists(filename):
        config.read(filename)

    # Clear existing hosts section or create if missing
    if 'hosts' not in config:
        config['hosts'] = {}
    else:
        # We want to clear keys in [hosts] but ConfigParser doesn't have clear(),
        # so we recreate the section or remove options.
        # Simplest is to remove section and re-add, but that might change order in file (usually fine).
        config.remove_section('hosts')
        config.add_section('hosts')

    # Add new hosts
    idx = 1
    for h in hosts_list:
        config.set('hosts', f'ip{idx}', h)
        idx += 1

    # Optionally write per-host section settings for hosts in the active list.
    if host_settings is None or not isinstance(host_settings, dict):
        host_settings = {}
    active_hosts = set(hosts_list)
    for host, settings in host_settings.items():
        if host not in active_hosts or not isinstance(settings, dict):
            continue
        if host not in config:
            config[host] = {}
        for k, v in settings.items():
            if v is None:
                continue
            config[host][str(k)] = str(v)

    # Note: We do NOT remove sections for devices that are no longer in the list.
    # The request was to "overwrite the old ini" regarding the SELECTION (device list).
    # Cleaning up unused sections is complex (is it unused or just not in the list currently?).
    # Users might want to keep the config for "192.168.1.50" even if it's not currently in the active list.
    # However, if the user says "so if i select no device at all the upload ini also will be empty",
    # they probably mean the DEVICE LIST is empty. The config sections are less visible.
    # Let's stick to updating [hosts].

    with open(filename, 'w') as f:
        config.write(f)




def parse_args() -> Tuple[Namespace, ConfigParser, ConfigParser, str]:
    """
    Function to parse all necessary parameters

    :returns:
        - _args - parsed args
        - _config - parsed upload.ini
        - _firmware - parsed version.ini
        - _my_ip - parsed own IP
    :rtype: Tuple[Namespace, ConfigParser, ConfigParser, str]
    """
    parser = ArgumentParser(
        description="Gude Systems firmware/config uploader (CLI + WebUI).",
        formatter_class=RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  1) List devices via UDP/GBL broadcast (status only):\n"
            "     upload.py -S -G\n"
            "  2) Update one device using online firmware:\n"
            "     upload.py -o -i 192.168.2.99\n"
            "  3) Start Web UI (no parameters):\n"
            "     upload.py\n"
        ),
    )
    parser.add_argument('-c', '--configip', help='ip address to select config')
    parser.add_argument('-f', '--forcefw', help='upload fw even if already up to date', action="store_true")
    parser.add_argument('-u', '--upload_ini', help='upload.ini paramater set', default='upload.ini')
    parser.add_argument('-v', '--version_ini', help='fw version defs', default='version.ini')
    parser.add_argument('-o', '--onlineupdate', help='use online update files', action="store_true", default=False)
    parser.add_argument('-i', '--iprange', nargs="+",  help='range of ip address to manage')
    parser.add_argument('-sf', '--search_folder', help='folder to search for binary')
    parser.add_argument('-r', '--repl_prod_id', help='product ids to replace', default={'2110': '2111', '8221': '822x', '8226': '822x'}) # , '8221': '822x', '8226': '822x'
    parser.add_argument('-d', '--devices', help="overwrites upload.ini like {'httpDefaults': {'port'=80, 'ssl'=0, 'auth'=0, 'username'='','password'=''} }", default=None, type=json.loads)
    # -d "{\"httpDefaults\":{\"username\":\"admin\",\"password\":\"admin\"}}"
    parser.add_argument('-H', '--header', help='Setting custom http header like \'{"Connection": "close"}\'', default=None, type=json.loads)
    parser.add_argument('-S', '--status', help='Only fetch device status without making any changes', action="store_true", default=False)
    parser.add_argument('-G', '--gbl', help='Use GBL broadcast', action="store_true", default=False)
    parser.add_argument('-ng', '--nogbl', help='Dont use GBL', action="store_true", default=False)
    parser.add_argument('--device-concurrency', type=int, default=1, help='Number of devices processed in parallel (default: 1)')
    parser.add_argument('--jsonl-progress', type=str, default=None, help='Write progress events to a JSONL file')
    parser.add_argument('--firmware-config', type=json.loads, default=None, help='JSON mapping of model->{filename, version} to override version.ini')
    parser.add_argument('--custom-config', type=json.loads, default=None, help='JSON mapping of ip->config_filename or "RESET" to override config file selection')
    parser.add_argument('--custom-ssl', type=json.loads, default=None, help='JSON mapping of ip->ssl_filename to override ssl cert selection')
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument('--debug', action='store_true', default=False, help='turn on debug log messages')
    verbosity.add_argument('--quiet', action='store_true', default=False, help='turn off info log messages')
    _args = parser.parse_args()
    log_config(_args.debug, _args.quiet)

    log.debug(f"Reading {_args.upload_ini} ...")
    _config = ConfigParser(strict=False)
    _read_files = _config.read(_args.upload_ini)
    _args.upload_ini_found = bool(_read_files)
    if not _args.upload_ini_found:
        log.warning(f"No upload.ini configuration found at '{_args.upload_ini}'.")
        log.warning("Provide hosts in upload.ini, enable 'gbl=search', use '--gbl', or pass '--iprange'.")

    if _args.gbl:
        log.debug(f"Detected GBL search flag, adding to config ...")
        if not _args.devices:
            _args.devices = {}
        if not 'hosts' in _args.devices:
            _args.devices['hosts'] = {}
        _args.devices['hosts'].update({'gbl':'search'})

    _config = add_devices_to_config(_args, _config)

    configure_auth_settings(_config)

    # Apply default settings using the generalized function
    for section, settings in DEFAULT_SETTINGS.items():
        set_config_defaults(_config, section, settings)

    _firmware = ConfigParser(strict=False)
    if _args.search_folder is not None and os.path.isdir(_args.search_folder):
         log.debug(f"Searching for binaries in {_args.search_folder} ...")
         bin_infos = file_search.rekursive_search(_args.search_folder)
         log.debug(f"Found {len(bin_infos)} binaries in {_args.search_folder} ...")
         unique_bin_infos = file_search.get_unique_devices(bin_infos)
         _firmware = file_search.get_config(unique_bin_infos, config=_firmware)
    elif _args.onlineupdate:
        _firmware = fetch_latest_fw_infos()
    else:
        log.debug(f"Reading {os.path.join(_config['defaults']['fwdir'], _args.version_ini)} ...")
        _firmware.read(os.path.join(_config['defaults']['fwdir'], _args.version_ini))
    log.debug("Getting my IP (for GBL/UDP search) ...")
    _my_ip = _config['defaults']['myIp'] if 'myIp' in _config['defaults'] else '0.0.0.0'

    return _args, _config, _firmware, _my_ip


def add_iprange_to_config(
    _iprange: Optional[List[str]],
    _config: ConfigParser,
    *,
    upload_ini_found: Optional[bool] = None,
    web_mode: bool = False,
): # Type hint for _iprange
    """
    Function that adds hosts from args to hosts in _config (parsed hosts from upload.ini)
    :param str _iprange: ip, ip-sub-net OR hostname
    :param ConfigParser _config: local properties parsed from upload.ini
    """
    # add cmd line arg to ip list
    if _iprange is not None:
        for i, ip_val in enumerate(_iprange): # Renamed ip to ip_val to avoid conflict
            log.debug(f"Adding iprange: {ip_val}")
            # Ensure 'hosts' section exists
            if 'hosts' not in _config:
                _config['hosts'] = {}
            if ':' in ip_val and '/' not in ip_val:
                if (']' in ip_val and ':' in ip_val.rpartition(']')[2]) or ']' not in ip_val:
                    _config[ip_val] = {} # This creates a section with the IP:Port as name
                    _config[ip_val]['port'] = ip_val.rpartition(':')[2]

            _config['hosts'][f'iprange_{i}'] = ip_val # Store in 'hosts' section
    else:
        if 'hosts' not in _config or not _config['hosts']: # Check if 'hosts' section is empty or not present
            error_lines = ["no EPC/PDU device IP address(es) given."]
            if upload_ini_found is False:
                error_lines.append("\tNo upload.ini configuration was found; no device list could be loaded.")
            else:
                error_lines.append("\tNo devices are configured in upload.ini.")

            if web_mode:
                error_lines.append("\tPlease enable 'gbl=search' in upload.ini or use 'Find Devices' in GUI,")
                error_lines.append("\tand/or give an IP address or subnet by --iprange parameter")
            else:
                error_lines.append("\tPlease try to enable 'gbl=search' or 'ip1=192.168.0.1' in your upload.ini,")
                error_lines.append("\tand/or give an IP address or subnet by --iprange parameter")

            _log_fn = log.warning if web_mode else log.error
            _log_fn("\n".join(error_lines) + "\n")
            if upload_ini_found is False:
                raise KeyError("Missing upload.ini configuration and no device target was provided.")
            raise KeyError("Missing required args, could not determine device!")


def generate_ip_list(_hosts_config: ConfigParser, _my_ip: str, _gbl_timeout: float) -> List[str]: # Changed _hosts to _hosts_config
    """
    Function that adds hosts from args to hosts in _config (parsed hosts from upload.ini)
    :param ConfigParser _hosts_config: ConfigParser section for hosts
    :param str _my_ip: this is the broadcaster/client ip, require for GBL/UDP search
    :param float _gbl_timeout: timeout in seconds to wait after sending broadcast

    :returns:
    - _ip_list - parsed args
    :rtype: list
    """
    _ip_list = []
    # Iterate over items in the 'hosts' section of the ConfigParser object
    log.debug(f"Getting all IPs for hosts defined in config section: hosts")
    for key, target in _hosts_config.items('hosts'): # Iterate over 'hosts' section
        log.debug(f"Checking host entry {key}: {target}")
        if target == "search":
            log.info("Searching devices by GBL UDP broadcast...")
            try:
                device_lst = Gblib.recv_bc(_my_ip, _gbl_timeout)
                for dev_info_bytes in device_lst: # dev is bytes
                    dev_info_dict = Gblib.get_dev_info(dev_info_bytes) # parse bytes to dict
                    if 'ip' in dev_info_dict:
                         _ip_list.append(dev_info_dict['ip'])
                    else:
                        log.warning(f"Device found via GBL without IP information: {dev_info_dict}")
            except Exception as e:
                log.error(f"Error during GBL search: {e}")
        else:
            num_hosts = 0
            try:
                # ipaddress.ip_network can handle single IPs as well if they are valid strings
                # It will create a network with a single host.
                # For hostnames, this will fail, and we'll fall into the ValueError.
                ip_network = ipaddress.ip_network(target, strict=False) # strict=False allows single IPs
                for ip_addr_obj in ip_network.hosts():
                    _ip_list.append(str(ip_addr_obj)) # Store as string
                    num_hosts += 1
                if num_hosts == 0 and ip_network.num_addresses == 1: # Single IP case
                     _ip_list.append(str(ip_network.network_address))
                     num_hosts = 1

            except ValueError: # Handles hostnames or invalid IP/network strings
                log.info(f"{target} could not be parsed as IPAddress or IP-Network by 'ipaddress' library, "
                            f"treating as a single host identifier (e.g., hostname or IP string).")
                _ip_list.append(target) # Add as is (could be hostname or IP string)
                num_hosts = 1

            if num_hosts == 0 and '/' not in target : # If still no hosts, and it wasn't a network string, add target itself
                 _ip_list.append(target)


    # Remove duplicates that might have occurred
    _ip_list = sorted(list(set(_ip_list)))

    # Advanced dedup: remove 'IP' if 'IP:PORT' exists
    # This prevents duplicates when GBL finds '1.2.3.4' but config has '1.2.3.4:80'
    final_list = []
    ip_map = {} # ip -> list of original strings

    for item in _ip_list:
        # Handle IPv4 primarily. IPv6 in simple logic might fail split(':') if not careful,
        # but code mostly handles IPv4.
        if item.count(':') == 1:
             ip_part = item.split(':')[0]
        else:
             ip_part = item

        if ip_part not in ip_map:
            ip_map[ip_part] = []
        ip_map[ip_part].append(item)

    for ip_part, items in ip_map.items():
        has_port_variant = any(':' in i for i in items)
        if has_port_variant:
            # Keep only those with ports. Discard the "bare" IP (usually from GBL)
            # as it is likely covered by the explicit config entry.
            for i in items:
                if ':' in i:
                    final_list.append(i)
        else:
             final_list.extend(items)

    return sorted(final_list)


def format_device_log_label(
    host: str,
    port: Optional[int] = None,
    use_ssl: bool = False,
    job_id: Optional[str] = None,
) -> str:
    endpoint = str(host)
    if port is not None:
        default_port = 443 if use_ssl else 80
        if port != default_port:
            endpoint = f"{endpoint}:{port}"
    return f"{job_id} {endpoint}" if job_id else endpoint


@dataclass
class DeviceResult:
    ip: str
    product_name: str
    mac: str
    initial_firmware: str
    hostname: Optional[str] = None
    # Connection details for UI linking
    conn_host: Optional[str] = None
    conn_port: Optional[int] = None
    conn_ssl: Optional[bool] = None
    url: Optional[str] = None
    latest_known_firmware: Optional[str] = None
    latest_publish_date: Optional[str] = None
    target_is_custom: bool = False
    selected_prodid: Optional[str] = None
    status_only: bool = False
    final_firmware: Optional[str] = None
    firmware_status: str = "not attempted"
    firmware_upload_notes: Optional[str] = None
    success: bool = False
    error_message: Optional[str] = None
    job_id: Optional[str] = None


def iterate_list(
    _ip_list: List[str],
    _firmware: ConfigParser,
    _config: ConfigParser,
    _args: object,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[DeviceResult]:
    """
    Function that iterates over all hosts from _ip_list hosts,
    matching corresponding firmware in _firmware,
    using http config given by _config,
    considering additional options from _args
    :param list _ip_list: list of ips, ip-sub-nets OR hostnames
    :param ConfigParser _firmware: containing firmware information
    :param ConfigParser _config: containing http config
    :param Namespace _args: additional options
    Returns a list of DeviceResult objects containing the processing status of each device.
    """

    # TODO: Is GBL in this context necessary?
    gbl = Gblib()

    concurrency = int(getattr(_args, 'device_concurrency', 1) or 1)
    use_progress_bar = (concurrency <= 1)
    show_job_id = (concurrency > 1)
    job_id_map = {
        str(ip_item): f"job-{idx:02d}"
        for idx, ip_item in enumerate(_ip_list, start=1)
    }

    results: List[DeviceResult] = [] # Type hint for results
    log.debug(f"trying {len(_ip_list)} devices")

    # Optional progress emitter
    def emit(evt: Dict[str, Any]):
        if progress_cb:
            try:
                progress_cb(evt)
            except Exception:
                pass

    def _process_device(ip_str_or_obj: Any) -> DeviceResult:
        ip = str(ip_str_or_obj) # Ensure ip is a string for consistency
        job_id = job_id_map.get(ip) if show_job_id else None
        result = DeviceResult(ip=ip, product_name="unknown", mac="unknown", initial_firmware="unknown", job_id=job_id)
        emit({"type": "device_start", "ip": ip, "job_id": job_id})

        def append_status_note(note: str) -> None:
            """Append an additional action summary to the visible per-device status text."""
            if not note:
                return
            if not result.firmware_status or result.firmware_status == "not attempted":
                result.firmware_status = note
                return
            if note in result.firmware_status:
                return
            result.firmware_status = f"{result.firmware_status}; {note}"

        try:
            log.debug(f"[{format_device_log_label(ip, job_id=job_id)}] Processing device")

            #if ip not in ["gwtestnet1.gude.local:38221", "gwtestnet1.gude.local:38226"]:
            #    continue

            dev_ip_for_conn = ip # Default
            # Handle potential port in IP string (though generate_ip_list should give clean IPs)
            if isinstance(ip, str) and ':' in ip:
                parts = ip.split(':')
                dev_ip_for_conn = parts[0]
                # Check if we're dealing with a hostname or IP
                try:
                    # Only validate if it looks like an IP address
                    if re.match(r'^[\d\.]+$', dev_ip_for_conn):
                        ipaddress.ip_address(dev_ip_for_conn)
                except ValueError:
                    # This is likely a hostname with port, which is fine
                    log.debug(f"Treating {dev_ip_for_conn} as hostname with port {parts[1]}")

            dev = DeployDev(dev_ip_for_conn, req_headers=_args.header)

            # Determine config_key: if ip (e.g. "192.168.1.10:8080") is a section name, use it.
            # Otherwise, use dev_ip_for_conn (e.g. "192.168.1.10") if it's a section.
            # Fallback to 'httpDefaults'.
            config_key_options = [ip, dev_ip_for_conn, 'httpDefaults']
            config_key = 'httpDefaults' # Default fallback
            for key_option in config_key_options:
                if _config.has_section(key_option):
                    config_key = key_option
                    break

            log.debug(f"[{format_device_log_label(ip, job_id=job_id)}] Using config-key '{config_key}'")

            # Determine connection settings
            # Avoid mutating shared config during concurrency
            port = int(_config.get(config_key, 'port', fallback=int(_config.get('httpDefaults', 'port', fallback=80))))
            use_ssl = _config.getboolean(config_key, 'ssl', fallback=_config.getboolean('httpDefaults', 'ssl', fallback=False))
            # Apply to device
            dev.set_http_port(port, use_ssl)
            dev.set_log_job_id(job_id)
            dev.set_basic_auth(_config.getboolean(config_key, 'auth', fallback=False),
                               _config.get(config_key, 'username', fallback=''),
                               _config.get(config_key, 'password', fallback=''))
            dev.set_http_timeout(float(_config.get('defaults', 'httpTimeout', fallback=3.0)))
            dev.set_http_retries(0)

            # Expose connection info for UI
            result.conn_host = dev_ip_for_conn
            result.conn_port = port
            result.conn_ssl = use_ssl
            proto = 'https' if use_ssl else 'http'
            result.url = f"{proto}://{dev_ip_for_conn}:{port}"

            mac = "unknown-mac"
            if not getattr(_args, 'nogbl', False):
                try:
                    log.info(f"[{dev.get_log_label()}] Attempting to get MAC via GBL/UDP...")
                    _gbl = Gblib()
                    if not _gbl.check_mac(str(dev_ip_for_conn)):
                        log.warning(f"[{dev.get_log_label()}] GBL timeout (UDP port 50123)")
                    else:
                        # Keep the historical config filename format: aa_bb_cc_dd_ee_ff.
                        mac = '_'.join(f'{c:02x}' for c in _gbl.dstMAC)
                        log.info(f"[{dev.get_log_label()}] Got MAC {mac} via GBL.")
                except (gaierror, ConnectionResetError, OSError) as e_gbl:
                    log.warning(f"[{dev.get_log_label()}] GBL MAC retrieval failed: {e_gbl}")
            else:
                log.debug(f"[{dev.get_log_label()}] GBL MAC lookup skipped by --nogbl.")
            result.mac = mac

            try:
                device_data = dev.http_get_status_json(DeployDev.JSON_STATUS_MISC)['misc']
            except HTTPError as e:
                if e.response.status_code == 401:
                    log.error(f"[{dev.get_log_label()}] Authentication error on configured port/protocol: {e}")
                    result.error_message = f"Authentication Error: {e}"
                    #continue
                    return result
                else:
                    log.error(f"[{dev.get_log_label()}] HTTPError: {e}")
                    result.error_message = f"HTTPError: {e}"
                    #continue # Re-raise if you want to stop all processing
                    return result
            except (Timeout, ConnectionError) as ce: # Catch Timeout and ConnectionError
                log.error(f"[{dev.get_log_label()}] Could not reach device on configured port/protocol: {ce}")
                result.error_message = f"Connection/Timeout Error: {ce}"
                #continue
                return result
            except Exception as e_misc: # Catch other potential errors during initial status fetch
                log.error(f"[{dev.get_log_label()}] Unexpected error fetching initial status: {e_misc}")
                result.error_message = f"Unexpected initial status error: {e_misc}"
                #continue
                return result

            result.product_name = device_data['product_name']
            result.hostname = device_data.get('hostname')
            result.initial_firmware = device_data['firm_v']

            if mac == "unknown-mac":
                log.info(f"[{dev.get_log_label()}] Falling back to HTTP(S) to get MAC...")
                try:
                    mac_http = dev.http_get_status_json(DeployDev.JSON_STATUS_ETHERNET, req_headers=None)['ethernet']['mac']
                    mac = mac_http.replace(':', '_')
                    log.info(f"[{dev.get_log_label()}] Got MAC {mac} via HTTP(S).")
                except (RequestException, ValueError, TimeoutError) as e_http_mac:
                    log.error(f"[{dev.get_log_label()}] Could not get MAC via HTTP(S): {e_http_mac}")
                    # Keep mac as "unknown-mac" or previous GBL error value

            result.mac = mac

            log.debug(f"[{dev.get_log_label()}] Getting config filename for MAC {mac}, IP {ip}...")

            # Check for custom config override
            custom_config_map = getattr(_args, 'custom_config', None) or {}
            # Try to find match by IP (exact or host:port if matches dev.host)
            custom_config_val = custom_config_map.get(ip)
            if not custom_config_val and dev.host in custom_config_map:
                custom_config_val = custom_config_map[dev.host]

            factory_reset_requested = (custom_config_val == "RESET")
            explicit_config_file = custom_config_val if (custom_config_val and not factory_reset_requested) else None

            # Pass explicit_filename if we have one (not RESET, and not None)
            cfg_filename = DeployDev.get_config_filename('config', 'config', 'txt', mac, ip, _args.configip, explicit_filename=explicit_config_file)

            log.debug(f"[{dev.get_log_label()}] Getting SSL certificate filename for MAC {mac}, IP {ip}...")

            # Check for custom ssl override
            custom_ssl_map = getattr(_args, 'custom_ssl', None) or {}
            custom_ssl_val = custom_ssl_map.get(ip)
            if not custom_ssl_val and dev.host in custom_ssl_map:
                custom_ssl_val = custom_ssl_map[dev.host]

            explicit_ssl_file = None
            skip_ssl = False
            if custom_ssl_val == "__no_cert__":
                skip_ssl = True
            elif custom_ssl_val:
                explicit_ssl_file = custom_ssl_val

            if skip_ssl:
                ssl_cert_filename = None
                log.info(f"[{dev.get_log_label()}] SSL upload skipped by user request.")
            else:
                ssl_cert_filename = DeployDev.get_config_filename('ssl', 'cert', 'pem', mac, ip, _args.configip, explicit_filename=explicit_ssl_file)

            # Check for custom firmware override (currently used for per-device "__no_update__")
            custom_firmware_map = getattr(_args, 'custom_firmware', None) or {}
            custom_firmware_val = custom_firmware_map.get(ip)
            if not custom_firmware_val and dev.host in custom_firmware_map:
                custom_firmware_val = custom_firmware_map[dev.host]
            skip_firmware_update = (custom_firmware_val == "__no_update__")

            # --- before logging/deploy ---
            actual_prod_id = device_data['prodid']
            # Build replacement map (if provided via args)
            repl_map = _args.repl_prod_id if isinstance(getattr(_args, 'repl_prod_id', None), dict) else None
            selected_prod_id, selected_version = resolve_prodid(
                actual_prodid=actual_prod_id,
                product_name=device_data.get('product_name') or '',
                firmware_cfg=_firmware,
                repl_map=repl_map,
            )
            # Offline fallback: if resolved section has no local file, try compatible
            # same-family entries (e.g. 80xxR2) that do have a local file.
            if selected_prod_id and not _args.onlineupdate:
                fw_dir_default = _config.get('defaults', 'fwdir', fallback='fw')
                fallback_prodid = find_offline_compatible_prodid(
                    selected_prodid=selected_prod_id,
                    firmware_cfg=_firmware,
                    fw_dir_default=fw_dir_default,
                )
                if fallback_prodid:
                    log.info(f"[{dev.get_log_label()}] Offline fallback: using compatible firmware section '{fallback_prodid}' instead of '{selected_prod_id}'")
                    selected_prod_id = fallback_prodid
                    selected_version = _firmware.get(selected_prod_id, 'version', fallback=selected_version)
                    if result.firmware_upload_notes:
                        result.firmware_upload_notes += f"; offline fallback -> {selected_prod_id}"
                    else:
                        result.firmware_upload_notes = f"offline fallback -> {selected_prod_id}"
            # update device_data and log
            if selected_prod_id:
                device_data["prodid"] = selected_prod_id # Update prodid for update_firmware call
                # Track selected product id for summary/use
                result.selected_prodid = selected_prod_id
                target_filename = _firmware.get(selected_prod_id, 'filename', fallback='').strip()
                result.target_is_custom = is_explicit_firmware_selection(target_filename)
                effective_target_version = selected_version
                if result.target_is_custom:
                    effective_target_version = resolve_configured_firmware_version(
                        selected_prod_id,
                        target_filename,
                        _firmware.get(selected_prod_id, 'version', fallback=''),
                    )
                disp_version = format_firmware_version_for_display(selected_prod_id, effective_target_version) or target_filename or "unknown"
                result.latest_known_firmware = disp_version
                # Determine publish date for latest known version (prefer model 'date' for online sources)
                try:
                    if result.target_is_custom:
                        result.latest_publish_date = None
                    elif _args.onlineupdate and _firmware.has_option(selected_prod_id, 'date'):
                        result.latest_publish_date = _firmware.get(selected_prod_id, 'date')
                    elif _firmware.has_section('url') and _firmware.has_option('url', 'last_update'):
                        result.latest_publish_date = _firmware.get('url', 'last_update')
                except Exception:
                    result.latest_publish_date = None
                firmware_label = "selected target" if result.target_is_custom else "latest known"
                log.info(
                    f"{device_data['product_name']} "
                    f"({actual_prod_id} -> {selected_prod_id}, {mac}) at {dev.get_log_label()}\n"
                    + " " * (len(f"{device_data['product_name']} ({actual_prod_id} -> {selected_prod_id}, {mac}) at {dev.get_log_label()}") - len(f"running Firmware v{device_data['firm_v']}")) # Align logging
                    + f"running Firmware v{device_data['firm_v']}, {firmware_label}: {result.latest_known_firmware}"
                )
            else:
                log.warning(f"[{dev.get_log_label()}] No firmware entry found for product '{actual_prod_id}' (original) or suitable replacement.")
                result.firmware_status = f"No firmware definition for {actual_prod_id}"

            # continue here for status
            if _args.status:
                result.firmware_status = "status only, no changes made"
                result.status_only = True
                result.success = False
                return result

            # deploy Firmware
            if skip_firmware_update:
                log.info(f"[{dev.get_log_label()}] Firmware update skipped by user request (No Update).")
                result.firmware_status = "Skipped (No Update)"
            elif selected_prod_id and selected_prod_id in _firmware: # Check if selected_prod_id is valid for _firmware
                # Check for explicit no-update flag
                target_filename = _firmware.get(selected_prod_id, 'filename', fallback='')
                if target_filename == '__no_update__':
                     log.info(f"[{dev.get_log_label()}] Firmware update skipped by user request (No Update).")
                     result.firmware_status = "Skipped (No Update)"
                else:
                    try:
                        fw_update_result = dev.update_firmware(device_data, _firmware,
                                                           _config.get('defaults', 'fwdir', fallback='fw'),
                                                           forced=_args.forcefw,
                                                           online_update=_args.onlineupdate,
                                                           show_progress_bar=use_progress_bar, progress_cb=emit)

                        result.final_firmware = fw_update_result.get("final_version", result.initial_firmware)
                        result.firmware_status = fw_update_result.get("status_message", "firmware update status unknown")
                        result.firmware_upload_notes = fw_update_result.get("upload_notes")
                    except ValueError as ve_fw: # Catches firmware file not found etc. from update_firmware
                        result.firmware_status = f"failed: {str(ve_fw)}"
                        log.warning(f"[{dev.get_log_label()}] Skipped firmware update: {ve_fw}")
                    except Exception as e_fw_update: # Catch any other unexpected error during update_firmware
                        result.firmware_status = f"failed: unexpected error during update ({str(e_fw_update)})"
                        log.error(f"[{dev.get_log_label()}] Unexpected error during firmware update: {e_fw_update}", exc_info=True)

            # Factory Reset Processing
            if factory_reset_requested:
                log.info(f"[{dev.get_log_label()}] Factory reset requested via custom config...")
                try:
                     if dev.factory_reset():
                         log.info(f"[{dev.get_log_label()}] Factory reset triggered successfully.")
                         result.firmware_status = "Factory Reset Triggered"
                         # Reset usually reboots the device, so we might want to skip further configuration
                         # But we continue to let valid flow happen if possible, though config upload likely moot
                     else:
                        log.warning(f"[{dev.get_log_label()}] Factory reset returned False (maybe not supported or failed).")
                        result.error_message = "Factory reset failed or not supported"
                except Exception as e_reset:
                     log.error(f"[{dev.get_log_label()}] Factory reset failed: {e_reset}")
                     result.error_message = f"Factory reset failed: {e_reset}"

            # Deploy Configuration
            # Skip config upload if we just did a factory reset?
            # Usually yes, unless user wants a specific config AFTER reset.
            # But the UI flow suggests either Reset OR Config, not both.
            if cfg_filename is not None and not factory_reset_requested:
                log.debug(f"[{dev.get_log_label()}] Attempting to upload configuration {cfg_filename}...")
                try:
                    dev.upload_config(cfg_filename, _args.configip, show_progress_bar=use_progress_bar, progress_cb=emit)
                    log.info(f"[{dev.get_log_label()}] Successfully uploaded configuration {cfg_filename}.")
                    append_status_note(f"Config updated ({cfg_filename})")
                except Exception as e_cfg:
                    log.error(f"[{dev.get_log_label()}] Failed to upload configuration {cfg_filename}: {e_cfg}")
                    if result.error_message: result.error_message += f"; Config upload error: {e_cfg}"
                    else: result.error_message = f"Config upload error: {e_cfg}"


            # Deploy SSL certificate
            if ssl_cert_filename is not None:
                log.debug(f"[{dev.get_log_label()}] Attempting to upload SSL certificate {ssl_cert_filename}...")
                try:
                    dev.upload_ssl_certificate(ssl_cert_filename, show_progress_bar=use_progress_bar, progress_cb=emit)
                    log.info(f"[{dev.get_log_label()}] Successfully uploaded SSL certificate {ssl_cert_filename}.")
                    append_status_note(f"SSL certificate updated ({ssl_cert_filename})")
                except Exception as e_ssl:
                    log.error(f"[{dev.get_log_label()}] Failed to upload SSL certificate {ssl_cert_filename}: {e_ssl}")
                    if result.error_message: result.error_message += f"; SSL cert upload error: {e_ssl}"
                    else: result.error_message = f"SSL cert upload error: {e_ssl}"

            # Print FW Version and configured Hostname (final check)
            try:
                final_ipv4_config = dev.http_get_config_json(dev.JSON_CONFIG_IP)['ipv4']
                final_misc_status = dev.http_get_status_json(DeployDev.JSON_STATUS_MISC)['misc']
                log.info(f"[{dev.get_log_label()}] Final check: hostname '{final_ipv4_config['hostname']}', FW Version {final_misc_status['firm_v']}")
                # Update final_firmware if it changed due to config/ssl reboot and wasn't from fw update
                if result.final_firmware == result.initial_firmware or result.final_firmware is None: # only if not set by fw update
                    if final_misc_status['firm_v'] != result.initial_firmware:
                        result.final_firmware = final_misc_status['firm_v']
                        # Potentially update firmware_status if it implies no update was done but version changed
                        if "not attempted" in result.firmware_status or "up to date" in result.firmware_status:
                             result.firmware_status = f"version changed to {result.final_firmware} (possibly due to other uploads)"


            except Exception as e_final_check:
                log.warning(f"[{dev.get_log_label()}] Could not perform final status check: {e_final_check}")

            # Overall success for the device if no major error message was set earlier
            if not result.error_message:
                 result.success = True # Mark as success if no critical errors were logged to result.error_message

        except Exception as e: # Catch-all for the processing of a single device
            log.error(f"[{format_device_log_label(ip, job_id=job_id)}] Major error processing device: {e}", exc_info=True)
            result.error_message = str(e)
            result.success = False # Ensure success is false on major error

        emit({
            "type": "device_done",
            "ip": ip,
            "job_id": job_id,
            "ok": result.success,
            "product": result.product_name,
            "initial_fw": result.initial_firmware,
            "final_fw": result.final_firmware,
            "status": result.firmware_status,
            "error": result.error_message,
        })
        return result

    if concurrency <= 1:
        for ip_str_or_obj in _ip_list:
            results.append(_process_device(ip_str_or_obj))
        return results

    # Concurrent execution
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        future_map = {pool.submit(_process_device, ip_obj): ip_obj for ip_obj in _ip_list}
        for fut in as_completed(future_map):
            try:
                results.append(fut.result())
            except Exception as e:
                ip_obj = future_map[fut]
                ip_key = str(ip_obj)
                results.append(
                    DeviceResult(
                        ip=ip_key,
                        product_name="unknown",
                        mac="unknown",
                        initial_firmware="unknown",
                        success=False,
                        error_message=str(e),
                        job_id=job_id_map.get(ip_key) if show_job_id else None,
                    )
                )
    return results


def configure_auth_settings(_config: ConfigParser) -> None:
    """
    Configure authentication settings for sections that have username and password but no auth flag set.

    :param _config: Configuration parser object to be edited
    """
    for section in _config.sections():
        # Ensure section is not 'DEFAULT' or other special sections if ConfigParser version implies
        if section == 'DEFAULT' or section == _config.default_section:
            continue

        has_user = _config.has_option(section, 'username') and _config.get(section, 'username', fallback=None) is not None
        has_pass = _config.has_option(section, 'password') and _config.get(section, 'password', fallback=None) is not None
        # Check if 'auth' is explicitly set to '0' or '1'. If not present, it's not "set".
        auth_val = _config.get(section, 'auth', fallback=None)
        has_auth_explicitly_set = auth_val in ['0', '1']


        if has_user and has_pass and not has_auth_explicitly_set:
            # mark this section as needing auth if username/password are non-empty
            if _config.get(section, 'username') or _config.get(section, 'password'):
                 log.debug(f"Configuring auth=1 for section '{section}' due to presence of username/password and no explicit auth setting.")
                 _config[section]['auth'] = '1'
            elif auth_val is None: # If username/password are empty and auth is not set at all
                 _config[section]['auth'] = '0' # Default to auth=0

def main() -> None:
    # get all args
    args, config, firmware, my_ip = parse_args()

    # parsing hosts
    add_iprange_to_config(args.iprange, config, upload_ini_found=getattr(args, 'upload_ini_found', None), web_mode=False)

    # Pass the 'hosts' section of config, not the whole config object
    ip_list = generate_ip_list(config, my_ip, float(config.get('defaults', 'gblTimeout', fallback=1.0)))

    # Optional JSONL progress writer
    progress_fp = None
    def _progress_cb(evt: Dict[str, Any]):
        if progress_fp:
            try:
                progress_fp.write(json.dumps(evt, ensure_ascii=False) + "\n")
                progress_fp.flush()
            except Exception:
                pass

    if getattr(args, 'jsonl_progress', None):
        try:
            progress_fp = open(args.jsonl_progress, 'a', encoding='utf-8')
        except Exception as e:
            log.warning(f"Could not open JSONL progress file {args.jsonl_progress}: {e}")
            progress_fp = None

    try:
        # iterate devices, get each dev info, update fw, config and certificate
        processing_results = iterate_list(ip_list, firmware, config, args, progress_cb=_progress_cb if progress_fp else None)
    finally:
        if progress_fp:
            try:
                progress_fp.close()
            except Exception:
                pass

    log.info("\nDevice Processing Summary:")
    log.info("-" * 80)
    for res_item in processing_results:
        status_char = "✓" if res_item.success else "✗"
        summary_endpoint = format_device_log_label(
            res_item.conn_host or res_item.ip,
            res_item.conn_port,
            bool(res_item.conn_ssl),
            res_item.job_id,
        )
        device_info = [
            f"{status_char} {summary_endpoint}",
            f"{res_item.product_name}",
            f"{res_item.mac}"
        ]

        # Label initial firmware: show "current" for status-only runs, otherwise "previous"
        initial_label = 'current' if getattr(res_item, 'status_only', False) else 'previous'
        device_fw = [f"{res_item.initial_firmware}({initial_label})"]

        if res_item.final_firmware and res_item.final_firmware != res_item.initial_firmware:
            device_fw.append(f"{res_item.final_firmware}(current)")
        else:
            if res_item.target_is_custom:
                device_fw.append(f"{res_item.latest_known_firmware}(selected)")
            else:
                # Prefer per-version publish date when available (esp. onlineupdate)
                last_update = res_item.latest_publish_date if res_item.latest_publish_date else (firmware["url"]["last_update"] if firmware.has_section("url") and firmware.has_option("url", "last_update") else "known")
                device_fw.append(f"{res_item.latest_known_firmware}(latest {last_update})")

        log.info(", ".join(device_info))
        log.info("   FW: "+", ".join(device_fw))

        if res_item.firmware_status:
            log.info(f"   Status: {res_item.firmware_status}")
        if res_item.firmware_upload_notes:
            log.info(f"   Notes: {res_item.firmware_upload_notes}")
        if res_item.error_message:
            log.info(f"   Error: {res_item.error_message}")

    log.info("-" * 80)
    success_count = sum(1 for r_item in processing_results if r_item.success)
    log.info(f"Successfully processed {success_count} of {len(processing_results)} devices (based on overall success flag).")


def run_processing_from_options(
    *,
    upload_ini: str = 'upload.ini',
    version_ini: str = 'version.ini',
    onlineupdate: bool = False,
    iprange: Optional[List[str]] = None,
    search_folder: Optional[str] = None,
    header: Optional[Dict[str, str]] = None,
    status: bool = False,
    gbl: bool = False,
    devices: Optional[Dict[str, Any]] = None,
    forcefw: bool = False,
    repl_prod_id: Optional[Dict[str, str]] = None,
    configip: Optional[str] = None,
    device_concurrency: int = 1,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
    firmware_config: Optional[Dict[str, Dict[str, str]]] = None,
    custom_firmware: Optional[Dict[str, str]] = None,
    custom_config: Optional[Dict[str, str]] = None,
    custom_ssl: Optional[Dict[str, str]] = None,
) -> List[DeviceResult]:
    """
    Programmatic entry-point to run the processing without CLI.

    Returns a list of DeviceResult for consumption by a web UI or API.
    """
    # Build a pseudo-args namespace compatible with iterate_list expectations
    args = Namespace()
    args.configip = configip
    args.forcefw = forcefw
    args.upload_ini = upload_ini
    args.version_ini = version_ini
    args.onlineupdate = onlineupdate
    args.iprange = iprange
    args.search_folder = search_folder
    args.repl_prod_id = repl_prod_id or {'2110': '2111'}
    args.devices = devices
    args.header = header
    args.status = status
    args.gbl = gbl
    # Keep parity with CLI: default to using GBL unless explicitly disabled
    args.nogbl = False
    # Concurrency for programmatic callers
    args.device_concurrency = int(device_concurrency or 1)
    args.custom_firmware = custom_firmware
    args.custom_config = custom_config
    args.custom_ssl = custom_ssl

    # Read upload.ini
    log.debug(f"[web] Reading {args.upload_ini} ...")
    config = ConfigParser(strict=False)
    read_files = config.read(args.upload_ini)
    args.upload_ini_found = bool(read_files)
    if not args.upload_ini_found:
        has_host_overrides = (
            isinstance(args.devices, dict) and
            isinstance(args.devices.get('hosts'), dict) and
            len(args.devices.get('hosts', {})) > 0
        )
        if has_host_overrides:
            log.debug(f"[web] No upload.ini configuration found at '{args.upload_ini}'. Using API-provided hosts.")
        else:
            log.warning(f"[web] No upload.ini configuration found at '{args.upload_ini}'.")
            log.warning("[web] Provide hosts in upload.ini, enable 'gbl=search', or use 'Find Devices' in GUI.")

    # Handle GBL flag similar to CLI path
    if args.gbl:
        log.debug("[web] Detected GBL search flag, adding to config ...")
        if not args.devices:
            args.devices = {}
        if 'hosts' not in args.devices:
            args.devices['hosts'] = {}
        args.devices['hosts'].update({'gbl': 'search'})

    # Merge device overrides
    config = add_devices_to_config(args, config)

    # If devices contain host entries like "host:port", ensure a matching
    # section exists with the extracted port so iterate_list can apply it.
    try:
        if config.has_section('hosts'):
            for _, target in config.items('hosts'):
                # Avoid CIDR; handle IPv6 [addr]:port or hostname:port
                if ':' in target and '/' not in target:
                    # If IPv6 in brackets, ensure colon is after ']'
                    tail = target.rpartition(']')[2] if ']' in target else target
                    if ':' in tail:
                        if not config.has_section(target):
                            config[target] = {}
                        config[target]['port'] = target.rpartition(':')[2]
    except Exception:
        # Do not fail the run if hosts parsing is odd; fall back to defaults
        pass

    # Configure authentication defaults
    configure_auth_settings(config)

    # Apply global defaults
    for section, settings in DEFAULT_SETTINGS.items():
        set_config_defaults(config, section, settings)

    # Prepare firmware database
    firmware = ConfigParser(strict=False)
    if args.search_folder is not None and os.path.isdir(args.search_folder):
        log.debug(f"[web] Searching for binaries in {args.search_folder} ...")
        bin_infos = file_search.rekursive_search(args.search_folder)
        unique_bin_infos = file_search.get_unique_devices(bin_infos)
        firmware = file_search.get_config(unique_bin_infos, config=firmware)
    elif args.onlineupdate:
        firmware = fetch_latest_fw_infos()
    else:
        log.debug(f"[web] Reading {os.path.join(config['defaults']['fwdir'], args.version_ini)} ...")
        firmware.read(os.path.join(config['defaults']['fwdir'], args.version_ini))

    # Apply firmware overrides if provided
    # Structure: {"80xx": {"filename": "my_firmware.bin", "version": "custom"}}
    if firmware_config:
        log.info(f"Applying firmware overrides: {firmware_config}")
        for model_section, overrides in firmware_config.items():
            # Ensure section exists (or create it if forcing a completely new model)
            if not firmware.has_section(model_section):
                # Optionally warn or creating might be risky if we don't know json/other params.
                # But for 'filename' override it might be enough if code only looks up filename.
                # However, resolve_prodid uses specific logic.
                firmware.add_section(model_section)

            for k, v in overrides.items():
                firmware.set(model_section, k, str(v))

            # Identify if we're forcing a specific file, ensure path/logic holds
            # deployDev uses 'filename' from this config.
            # It also checks 'version' to decide if update is needed.
            # If we set 'version' to 'force_update_custom', it likely triggers update.

    # Determine own IP for GBL/UDP search
    my_ip = config['defaults']['myIp'] if 'myIp' in config['defaults'] else '0.0.0.0'

    # Build IP list and run processing
    add_iprange_to_config(
        args.iprange,
        config,
        upload_ini_found=getattr(args, 'upload_ini_found', None),
        web_mode=True,
    )
    ip_list = generate_ip_list(config, my_ip, float(config.get('defaults', 'gblTimeout', fallback=2.0)))
    results = iterate_list(ip_list, firmware, config, args, progress_cb=progress_cb)
    return results


if __name__ == "__main__":  # Ensure this runs only when script is executed directly
    # If no CLI arguments are given, launch the Web UI server and open browser
    if len(sys.argv) <= 1:
        log_config(debug=False, quiet=False)
        try:
            from webui.server import serve
            # Bind only on localhost and open browser to localhost
            serve(host='127.0.0.1', port=8000, open_browser=True)
        except Exception as e:
            print(f"Failed to start Web UI server: {e}")
    else:
        main()
