"""Turn wizard answers into a Raspberry Pi OS boot partition that sets itself up
as a BrandMeister DMRHost hotspot on first boot.

The card is flashed with stock Raspberry Pi OS Lite (64-bit, trixie or newer),
and this module only writes to its FAT boot partition, which every desktop OS
mounts by itself. Shared by the browser wizard and the command line (wizard.py).
Python standard library only.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import ssl
import string
import sys
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSION = "0.2.0"
MASTERS_URL = "https://api.brandmeister.network/v2/master/"
RADIOID_URL = "https://radioid.net/api/dmr/user/?callsign="
MASTER_PORT = 62031
MARK_BEGIN = "# >>> dmrhost-installer"
MARK_END = "# <<< dmrhost-installer"
USER_GROUPS = "users,adm,dialout,audio,netdev,video,plugdev,input,gpio,spi,i2c,sudo"
OLED_TYPES = {"ssd1306": 3, "sh1106": 6}


def _tls() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # A bundled binary's OpenSSL looks for CA certificates where its build machine keeps
    # them, which other Linux distros and macOS don't have: add certifi's, bundled at build
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def resource_dir() -> Path:
    # PyInstaller unpacks bundled files to sys._MEIPASS
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


PAYLOAD = resource_dir() / "payload"


# --- questions ----------------------------------------------------------------
# One list drives both the browser form and the command line. "when" shows a
# field only if another answer has that value.

SECTIONS = [
    {"id": "station", "title": "Station", "fields": [
        {"key": "callsign", "label": "Callsign", "kind": "text", "required": True, "upper": True,
         "placeholder": "N0CALL"},
        {"key": "dmr_id", "label": "DMR ID", "kind": "text", "required": True, "placeholder": "1234567",
         "help": "Your 7-digit ID from radioid.net."},
        {"key": "id_suffix", "label": "ESSID", "kind": "text", "default": "01",
         "help": "Two digits that make the hotspot's ID: 1234567 + 01 = 123456701."},
        {"key": "master", "label": "BrandMeister master", "kind": "master", "required": True,
         "help": "Usually the one for your country: search by country, code or ID. Or type an address, optionally with :port."},
        {"key": "bm_password", "label": "Hotspot security password", "kind": "password", "required": True,
         "warn": "This is NOT your BrandMeister account password. It is a different password, set in SelfCare.",
         "link": "https://brandmeister.network/#/selfcare",
         "help": "Set in BrandMeister SelfCare, under your profile's hotspot security."},
        {"key": "location", "label": "Location", "kind": "text", "placeholder": "Lisbon"},
        {"key": "url", "label": "URL", "kind": "text",
         "help": "Leave blank for your QRZ.com page."},
    ]},
    {"id": "radio", "title": "Radio", "fields": [
        {"key": "hat", "label": "Hat", "kind": "choice", "default": "duplex", "choices": [
            ["duplex", "MMDVM_HS duplex", "Two frequencies, both timeslots"],
            ["simplex", "MMDVM_HS simplex", "One frequency, timeslot 2 only"]]},
        {"key": "rx_freq", "label": "Hotspot receives on (MHz)", "kind": "freq", "required": True,
         "placeholder": "432.000", "help": "Your radio transmits on this one."},
        {"key": "tx_freq", "label": "Hotspot transmits on (MHz)", "kind": "freq", "required": True,
         "placeholder": "434.000", "help": "Your radio receives on this one.", "when": ["hat", "duplex"]},
        {"key": "rx_offset", "label": "RX offset (Hz)", "kind": "text", "default": "0",
         "help": "Leave at 0 to start. Tune it later from the dashboard if your BER is high."},
        {"key": "color_code", "label": "Colour code", "kind": "text", "default": "1"},
        {"key": "oled", "label": "Display", "kind": "choice", "default": "ssd1306", "choices": [
            ["ssd1306", "0.96\" OLED", "SSD1306, the usual one on duplex hats"],
            ["sh1106", "1.3\" OLED", "SH1106"],
            ["none", "No display", ""]]},
    ]},
    {"id": "pi", "title": "Pi", "fields": [
        {"key": "hostname", "label": "Hostname", "kind": "text", "default": "dmrhost",
         "help": "Reach it as <hostname>.local."},
        {"key": "username", "label": "Username", "kind": "text", "required": True},
        {"key": "ssh_key", "label": "SSH public key", "kind": "textarea",
         "help": "Found in ~/.ssh/*.pub. Give a key, a password, or both."},
        {"key": "password", "label": "Password", "kind": "password",
         "help": "Optional with a key. Also allows SSH logins with the password."},
        {"key": "timezone", "label": "Time zone", "kind": "text", "required": True, "placeholder": "Europe/Lisbon"},
        {"key": "wifi_ssid", "label": "Wi-Fi network", "kind": "text",
         "help": "A 2.4 GHz network: a Pi Zero 2 W can't see 5 GHz. Leave blank to use Ethernet only (a Pi Zero has none)."},
        {"key": "wifi_password", "label": "Wi-Fi password", "kind": "password", "when": ["wifi_ssid", True]},
        {"key": "wifi_country", "label": "Wi-Fi country", "kind": "text", "upper": True, "placeholder": "PT",
         "help": "Two-letter code; sets the legal Wi-Fi channels.", "when": ["wifi_ssid", True]},
    ]},
    {"id": "dashboard", "title": "Dashboard", "fields": [
        {"key": "dashboard", "label": "Install dmrdash", "kind": "bool", "default": True,
         "help": "Live calls and talkgroup management at http://<hostname>.local:8080."},
        {"key": "api_key", "label": "SelfCare API key", "kind": "password", "when": ["dashboard", True],
         "help": "Optional. Needed to change talkgroups from the dashboard; add it later in /etc/dmrdash.conf.",
         "find": "On brandmeister.network: your callsign (top right) → Profile settings → Security settings → API keys.",
         "find_link": "https://brandmeister.network/#/profile/{callsign}/api", "find_link_text": "Open your API keys page"},
        {"key": "pin", "label": "Dashboard PIN", "kind": "password", "when": ["dashboard", True],
         "help": "Optional. Asked for before any change."},
        {"key": "port", "label": "Dashboard port", "kind": "text", "default": "8080", "when": ["dashboard", True]},
    ]},
]

FIELDS = {f["key"]: f for s in SECTIONS for f in s["fields"]}


def find_link(field: dict, answers: dict) -> str:
    """A field's "where to find it" link, with the callsign filled in."""
    call = re.sub(r"[^A-Za-z0-9]", "", str(answers.get("callsign", ""))).upper()
    link = field.get("find_link", "")
    return link.replace("{callsign}", call) if call or "{callsign}" not in link else ""


def visible(field: dict, answers: dict) -> bool:
    if "when" not in field:
        return True
    key, want = field["when"]
    have = answers.get(key)
    return bool(have) if want is True else have == want


# --- saved progress ---------------------------------------------------------------
# The wizard's answers survive closing the browser. Secrets (every "password" field)
# are never written: they would sit in a plain file long after the card is done.

def progress_path() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dmrhost-installer" / "progress.json"


def save_progress(answers: dict, step: int) -> None:
    keep = {k: v for k, v in answers.items()
            if k in FIELDS and FIELDS[k]["kind"] != "password"
            and isinstance(v, (str, bool)) and len(str(v)) <= 4096}
    path = progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": VERSION, "step": max(0, min(int(step), len(SECTIONS))), "answers": keep}, f, indent=2)
    os.replace(tmp, path)


def load_progress() -> dict | None:
    try:
        data = json.loads(progress_path().read_text(encoding="utf-8"))
        answers = {k: v for k, v in data["answers"].items()
                   if k in FIELDS and FIELDS[k]["kind"] != "password" and isinstance(v, (str, bool))}
        mtime = progress_path().stat().st_mtime
        return {"step": int(data.get("step", 0)), "answers": answers, "saved": mtime}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clear_progress() -> None:
    try:
        progress_path().unlink()
    except OSError:
        pass


def defaults() -> dict:
    d = {k: f.get("default", "") for k, f in FIELDS.items()}
    user = getpass.getuser().lower()
    d["username"] = user if re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) and user != "root" else "pi"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        p = Path.home() / ".ssh" / name
        if p.is_file():
            d["ssh_key"] = p.read_text(encoding="utf-8", errors="replace").strip()
            break
    d["timezone"] = local_timezone()
    return d


def local_timezone() -> str:
    tz = os.environ.get("TZ", "").lstrip(":")
    if "/" in tz and not tz.startswith("/"):
        return tz
    try:
        real = os.path.realpath("/etc/localtime")
    except OSError:
        real = ""
    if "zoneinfo/" in real:
        return real.split("zoneinfo/", 1)[1]
    return "UTC"


# --- BrandMeister masters -------------------------------------------------------

_masters_cache: list | None = None


def fetch_masters(timeout: float = 8) -> list:
    """[{"id": 2682, "country": "PT", "address": "..."}...] sorted by id; [] when offline."""
    global _masters_cache
    if _masters_cache is None:
        try:
            req = urllib.request.Request(MASTERS_URL, headers={"User-Agent": f"dmrhost-installer/{VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout, context=_tls()) as r:
                rows = json.load(r)
            _masters_cache = sorted(
                ({"id": int(m["id"]), "country": str(m.get("country", "")), "address": str(m["address"])}
                 for m in rows if m.get("address")),
                key=lambda m: m["id"])
        except (OSError, ValueError, KeyError, TypeError):
            return []
    return _masters_cache


def resolve_master(value: str, masters: list) -> tuple[str, int] | None:
    value = value.strip()
    if value.isdigit():
        for m in masters:
            if m["id"] == int(value):
                return m["address"], MASTER_PORT
        return None
    host, _, port = value.partition(":")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", host or "-") or (port and not port.isdigit()):
        return None
    return host, int(port) if port else MASTER_PORT


# --- radioid.net ----------------------------------------------------------------

_ids_cache: dict = {}


def lookup_ids(callsign: str, timeout: float = 8) -> list:
    """The DMR IDs radioid.net has for a callsign, most recently heard first; [] when
    there are none or it can't be reached."""
    call = callsign.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,8}", call):
        return []
    if call not in _ids_cache:
        try:
            req = urllib.request.Request(RADIOID_URL + urllib.parse.quote(call),
                                         headers={"User-Agent": f"dmrhost-installer/{VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout, context=_tls()) as r:
                rows = json.load(r).get("results") or []
        except (OSError, ValueError, AttributeError):
            return []

        def heard(row):
            try:
                return parsedate_to_datetime(row["lastheard"]).timestamp()
            except (KeyError, TypeError, ValueError):
                return 0

        _ids_cache[call] = [
            {"id": int(row["id"]), "name": " ".join(x for x in (row.get("fname"), row.get("surname")) if x),
             "city": row.get("city") or "", "country": row.get("country") or "",
             "lastheard": row.get("lastheard") or "", "lastmaster": str(row.get("lastmaster") or "")}
            for row in sorted(rows, key=heard, reverse=True)
            if str(row.get("callsign", "")).upper() == call and str(row.get("id", "")).isdigit()]
    return _ids_cache[call]


# --- importing a WPSD or Pi-Star backup -------------------------------------------
# The dashboards' "Backup/Restore" zip holds /etc/mmdvmhost, /etc/dmrgateway, the
# Wi-Fi settings and more, flat at its top level. Only answers come out of it: the
# zip is read in memory and nothing in it is run or written anywhere.

BACKUP_MAX = 16 * 1024 * 1024
BACKUP_FILES = ("mmdvmhost", "dmrgateway", "wpa_supplicant.conf", "hostname", "bmapi.key",
                "config.php", "WPSD-Dashboard-Config.ini")


def _ini(text: str) -> dict:
    """A forgiving INI reader: {section: {key: value}}, keys lower-cased, first value wins."""
    out, sec = {}, None
    for ln in text.splitlines():
        ln = ln.strip()
        if ln.startswith("[") and ln.endswith("]"):
            sec = out.setdefault(ln[1:-1].strip().lower(), {})
        elif sec is not None and "=" in ln and not ln.startswith(("#", ";")):
            k, v = ln.split("=", 1)
            sec.setdefault(k.strip().lower(), v.strip())
    return out


def _unquote(v: str) -> str:
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def _hz_to_mhz(v: str) -> str:
    if not v.isdigit():
        return ""
    mhz = f"{int(v) / 1e6:.6f}".rstrip("0")
    return mhz + "0" * (3 - len(mhz.partition(".")[2]))


def _wpa_networks(text: str) -> tuple[list, str]:
    """([{ssid, psk, priority}...], country) from a wpa_supplicant.conf."""
    country = (re.search(r"(?m)^\s*country\s*=\s*([A-Za-z]{2})\s*$", text) or [None, ""])[1]
    nets = []
    for block in re.findall(r"network\s*=\s*\{(.*?)\}", text, flags=re.S):
        kv = dict(re.findall(r"(?m)^\s*(\w+)\s*=\s*(.*?)\s*$", block))
        ssid = kv.get("ssid", "")
        if ssid.startswith('"'):
            ssid = _unquote(ssid)
        elif re.fullmatch(r"(?:[0-9a-fA-F]{2})+", ssid):  # an SSID written as hex bytes
            ssid = bytes.fromhex(ssid).decode("utf-8", "replace")
        if not ssid or kv.get("disabled") == "1":
            continue
        psk = kv.get("psk", "")
        psk = _unquote(psk) if psk.startswith('"') else psk if re.fullmatch(r"[0-9a-fA-F]{64}", psk) else ""
        prio = kv.get("priority", "0")
        nets.append({"ssid": ssid, "psk": psk, "priority": int(prio) if prio.lstrip("-").isdigit() else 0})
    nets.sort(key=lambda n: -n["priority"])  # stable: the first of equals stays first
    return nets, country.upper()


def read_backup(data: bytes, masters: list | None = None) -> dict:
    """Wizard answers from a WPSD or Pi-Star configuration backup (zip).
    Returns {"answers": {...}, "notes": [...]}; raises ValueError if it isn't one."""
    import io
    import zipfile
    if len(data) > BACKUP_MAX:
        raise ValueError("That file is too big to be a WPSD or Pi-Star backup.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That isn't a zip file. In WPSD or Pi-Star, use Configuration → Backup/Restore → Download.")
    files, nm = {}, []
    with z:
        for info in z.infolist():
            # by base name, so a backup that was unpacked and zipped again into a folder works too
            name = info.filename.replace("\\", "/").rsplit("/", 1)[-1]
            if info.is_dir() or info.file_size > 1024 * 1024:
                continue
            if name in BACKUP_FILES or name.endswith(".nmconnection"):
                try:
                    text = z.read(info).decode("utf-8", "replace")
                except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
                    continue  # encrypted or damaged: skip that one file
                if name.endswith(".nmconnection"):
                    nm.append(text)
                else:
                    files.setdefault(name, text)
    if "mmdvmhost" not in files:
        raise ValueError("No mmdvmhost settings in that zip. Is it a WPSD or Pi-Star configuration backup?")

    a, notes = {}, []
    mm = _ini(files["mmdvmhost"])
    gen, info, modem, dmr = (mm.get(s, {}) for s in ("general", "info", "modem", "dmr"))
    call = gen.get("callsign", "").upper()
    if re.fullmatch(r"[A-Z0-9]{3,8}", call):
        a["callsign"] = call
    hid = gen.get("id") or dmr.get("id") or ""
    if re.fullmatch(r"[1-9][0-9]{6,8}", hid):
        a["dmr_id"] = hid[:7]
        if len(hid) == 9:
            a["id_suffix"] = hid[7:]
        elif len(hid) == 8:
            a["id_suffix"] = "0" + hid[7]
        else:
            notes.append(f"The old hotspot used your plain DMR ID, {hid}. This one adds the ESSID: "
                         f"{hid}{FIELDS['id_suffix']['default']} unless you change it.")
    duplex = gen.get("duplex", "1") != "0"
    a["hat"] = "duplex" if duplex else "simplex"
    rx, tx = _hz_to_mhz(info.get("rxfrequency", "")), _hz_to_mhz(info.get("txfrequency", ""))
    if rx:
        a["rx_freq"] = rx
    if tx and duplex:
        a["tx_freq"] = tx
    for key, src in (("location", info.get("location", "")), ("url", info.get("url", ""))):
        if src and not re.search(r"[\x00-\x1f\x7f]", src):
            a[key] = src
    if re.fullmatch(r"[+-]?[0-9]{1,5}", modem.get("rxoffset", "")):
        a["rx_offset"] = modem["rxoffset"]
    if dmr.get("colorcode", "").isdigit():
        a["color_code"] = dmr["colorcode"]
    display = gen.get("display", "").lower()
    if display == "oled":
        a["oled"] = {"6": "sh1106"}.get(mm.get("oled", {}).get("type", ""), "ssd1306")
    elif display:
        a["oled"] = "none"
        if display not in ("none", ""):
            notes.append(f"The old hotspot drove a {gen['display']} display; this installer supports OLEDs only.")

    # BrandMeister: straight from MMDVMHost, or through DMRGateway (127.0.0.1)
    net = mm.get("dmr network", {})
    address, port, password, name = net.get("address", ""), net.get("port", ""), net.get("password", ""), ""
    if address in ("127.0.0.1", "localhost", "") and "dmrgateway" in files:
        gw = _ini(files["dmrgateway"])
        for sec in sorted(s for s in gw if re.fullmatch(r"dmr network \d+", s)):
            g = gw[sec]
            if g.get("enabled", "1") != "0" and (g.get("name", "").upper().startswith("BM")
                                                 or "brandmeister" in g.get("address", "").lower()):
                address, port, password, name = g.get("address", ""), g.get("port", ""), g.get("password", ""), g.get("name", "")
                break
        else:
            address = ""
    password = _unquote(password)
    if password and password.lower() != "passw0rd":
        a["bm_password"] = password
    if address and address not in ("127.0.0.1", "localhost"):
        masters = masters if masters is not None else fetch_masters()
        by_name = re.match(r"BM_(\d{4})", name)
        hit = next((m for m in masters if m["address"] == address), None)
        if hit:
            a["master"] = str(hit["id"])
        elif by_name and any(m["id"] == int(by_name[1]) for m in masters):
            a["master"] = by_name[1]
        elif re.fullmatch(r"[A-Za-z0-9.-]+", address):
            a["master"] = address + (f":{port}" if port.isdigit() and int(port) != MASTER_PORT else "")
    if "bm_password" not in a:
        notes.append("No hotspot security password in the backup: type it on the Station step.")

    host = files.get("hostname", "").strip().lower()
    if re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", host):
        a["hostname"] = host
    for text in (files.get("WPSD-Dashboard-Config.ini", ""), files.get("config.php", "")):
        m = re.search(r"date_default_timezone_set\(\s*['\"]([^'\"]+)['\"]", text) or \
            re.search(r"(?im)^\s*time_?zone\s*=\s*['\"]?([A-Za-z_]+/[A-Za-z0-9_/+-]+)", text)
        if m:
            a["timezone"] = m[1]
            break

    nets, country = _wpa_networks(files.get("wpa_supplicant.conf", ""))
    for text in nm:  # NetworkManager profiles (WPSD on bookworm)
        c = _ini(text)
        if c.get("wifi", {}).get("ssid"):
            nets.append({"ssid": c["wifi"]["ssid"], "psk": c.get("wifi-security", {}).get("psk", ""), "priority": 0})
    if nets:
        a["wifi_ssid"], a["wifi_password"] = nets[0]["ssid"], nets[0]["psk"]
        if country:
            a["wifi_country"] = country
        if len(nets) > 1:
            notes.append(f"The backup has {len(nets)} Wi-Fi networks; this sets up {nets[0]['ssid']} only.")

    key = re.search(r"(?m)^\s*apikey\s*=\s*(\S+)", files.get("bmapi.key", ""))
    if key and key[1].lower() != "none":
        a["api_key"] = key[1]
    notes.append("A backup has no logins for this installer: choose your username, SSH key or password on the Pi step.")
    return {"answers": a, "notes": notes}


# --- validation -----------------------------------------------------------------

def _mhz(text: str) -> int | None:
    try:
        hz = round(float(text.replace(",", ".")) * 1_000_000)
    except ValueError:
        return None
    return hz if 130_000_000 <= hz <= 960_000_000 else None


def validate(raw: dict, masters: list | None = None) -> tuple[dict, dict]:
    """Return (answers, errors). errors maps a field key to a message."""
    a, err = {}, {}
    for k, f in FIELDS.items():
        v = raw.get(k, f.get("default", ""))
        if f["kind"] == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "y", "on")
        else:
            v = str(v if v is not None else "").strip()
            if f.get("upper"):
                v = v.upper()
        a[k] = v
    for k, f in FIELDS.items():
        v = a[k]
        if not visible(f, a):
            continue
        if f.get("required") and v == "":
            err[k] = "Required."
        elif isinstance(v, str) and k not in ("ssh_key",) and re.search(r"[\x00-\x1f\x7f]", v):
            err[k] = "No line breaks or control characters."

    def check(key, ok, msg):
        if key not in err and a[key] != "" and not ok:
            err[key] = msg

    check("callsign", re.fullmatch(r"[A-Z0-9]{3,8}", a["callsign"]), "Letters and digits only.")
    check("dmr_id", re.fullmatch(r"[1-9][0-9]{6}", a["dmr_id"]), "Seven digits.")
    check("id_suffix", re.fullmatch(r"[0-9]{2}", a["id_suffix"]) and a["id_suffix"] != "00",
          "Two digits, 01 to 99.")
    if a["master"] and "master" not in err:
        m = resolve_master(a["master"], masters if masters is not None else fetch_masters())
        if m:
            a["master_address"], a["master_port"] = m
        else:
            err["master"] = "Pick a master from the list, or type an address."
    check("url", re.fullmatch(r"https?://\S+", a["url"]), "Starts with http:// or https://.")
    for k in ("rx_freq", "tx_freq"):
        if visible(FIELDS[k], a):
            check(k, _mhz(a[k]), "A frequency in MHz, like 432.000.")
    if a["hat"] == "duplex" and "rx_freq" not in err and "tx_freq" not in err and a["rx_freq"] and \
            _mhz(a["rx_freq"]) == _mhz(a["tx_freq"]):
        err["tx_freq"] = "A duplex hat needs two different frequencies."
    check("rx_offset", re.fullmatch(r"[+-]?[0-9]{1,5}", a["rx_offset"]), "Whole hertz, like -350.")
    check("color_code", a["color_code"].isdigit() and 0 <= int(a["color_code"]) <= 15, "0 to 15.")
    if "hat" not in err and a["hat"] not in ("duplex", "simplex"):
        err["hat"] = "Duplex or simplex."
    if a["oled"] not in ("ssd1306", "sh1106", "none"):
        err["oled"] = "Pick a display."
    check("hostname", re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", a["hostname"]),
          "Lower-case letters, digits and hyphens.")
    a["hostname"] = a["hostname"] or "dmrhost"
    check("username", re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", a["username"]) and a["username"] != "root",
          "Lower-case letters, digits, _ and -, and not root.")
    keys = [ln.strip() for ln in a["ssh_key"].splitlines() if ln.strip() and not ln.startswith("#")]
    bad = [ln for ln in keys if not re.fullmatch(r"(ssh-|ecdsa-|sk-)\S+ [A-Za-z0-9+/=]+( .*)?", ln)]
    if bad:
        err["ssh_key"] = "That doesn't look like a public key (ssh-ed25519 AAAA...)."
    a["ssh_keys"] = keys
    if not keys and not a["password"] and "ssh_key" not in err:
        err["password"] = "Give an SSH key or a password, or you can't log in."
    check("password", len(a["password"]) >= 8, "At least 8 characters.")
    check("timezone", re.fullmatch(r"UTC|[A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2}", a["timezone"]),
          "Like Europe/Lisbon.")
    if a["wifi_ssid"]:
        check("wifi_ssid", len(a["wifi_ssid"].encode()) <= 32, "At most 32 bytes.")
        # 64 hex digits is the hashed key wpa_passphrase writes, as Pi-Star and WPSD store it
        check("wifi_password", 8 <= len(a["wifi_password"]) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", a["wifi_password"]),
              "8 to 63 characters, or blank for an open network.")
        if not a["wifi_country"]:
            err["wifi_country"] = "Required with Wi-Fi."
        check("wifi_country", re.fullmatch(r"[A-Z]{2}", a["wifi_country"]), "Two letters, like PT.")
    if a["dashboard"]:
        check("port", a["port"].isdigit() and 1 <= int(a["port"]) <= 65535, "1 to 65535.")
        check("pin", re.fullmatch(r"\S{4,}", a["pin"]), "At least 4 characters, no spaces.")
    return a, err


# --- rendering ------------------------------------------------------------------

def q(s: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def hotspot_id(a: dict) -> str:
    return a["dmr_id"] + a["id_suffix"]


def mmdvm_ini(a: dict) -> str:
    duplex = a["hat"] == "duplex"
    rx = _mhz(a["rx_freq"])
    tx = _mhz(a["tx_freq"]) if duplex else rx
    tpl = string.Template((PAYLOAD / "MMDVM.ini.in").read_text(encoding="utf-8"))
    return tpl.substitute(
        callsign=a["callsign"], hotspot_id=hotspot_id(a), duplex=int(duplex),
        rx_hz=rx, tx_hz=tx, location=a["location"],
        description="MMDVM_HS Duplex" if duplex else "MMDVM_HS Simplex",
        url=a["url"] or f"https://www.qrz.com/db/{a['callsign']}",
        rx_offset=int(a["rx_offset"]), color_code=int(a["color_code"]),
        master_address=a["master_address"], master_port=a["master_port"], bm_password=a["bm_password"],
        slot1=int(duplex), display_enable=int(a["oled"] != "none"), oled_type=OLED_TYPES.get(a["oled"], 3))


def user_data(a: dict) -> str:
    lines = [
        "#cloud-config",
        f"# Written by dmrhost-installer {VERSION}. Runs once, on the first boot.",
        f"hostname: {q(a['hostname'])}",
        "manage_etc_hosts: true",
        f"timezone: {q(a['timezone'])}",
        "locale: en_US.UTF-8",
        "",
        "users:",
        f"- name: {q(a['username'])}",
        f"  groups: {USER_GROUPS}",
        "  shell: /bin/bash",
        "  sudo: ALL=(ALL) NOPASSWD:ALL",
        "  lock_passwd: true",
    ]
    if a["ssh_keys"]:
        lines.append("  ssh_authorized_keys:")
        lines += [f"  - {q(k)}" for k in a["ssh_keys"]]
    lines += [
        "",
        f"ssh_pwauth: {'true' if a['password'] else 'false'}",
        "",
        "runcmd:",
        "- [ systemctl, enable, --now, ssh ]",
        "# the rest happens in dmrhost-firstboot.service, which retries until it is online",
        "- [ sh, /boot/firmware/dmrhost-setup/firstboot.sh, --stage ]",
        "",
    ]
    return "\n".join(lines)


def network_config(a: dict) -> str:
    lines = ["network:", "  version: 2", "  ethernets:", "    eth0:", "      dhcp4: true",
             f"      optional: {'true' if a['wifi_ssid'] else 'false'}"]
    if a["wifi_ssid"]:
        lines += ["  wifis:", "    wlan0:", "      dhcp4: true", "      optional: false",
                  f"      regulatory-domain: {q(a['wifi_country'])}", "      access-points:"]
        if a["wifi_password"]:
            lines += [f"        {q(a['wifi_ssid'])}:", f"          password: {q(a['wifi_password'])}"]
        else:
            lines.append(f"        {q(a['wifi_ssid'])}: {{}}")
    return "\n".join(lines) + "\n"


def meta_data() -> str:
    return f"dsmode: local\ninstance_id: dmrhost-{secrets.token_hex(4)}\n"


def setup_env(a: dict) -> str:
    # every value here is validated to be shell-safe
    return (f"CALLSIGN={a['callsign']}\nLOGIN_USER={a['username']}\n"
            f"OLED={int(a['oled'] != 'none')}\nDASHBOARD={int(a['dashboard'])}\n")


def dmrdash_conf(a: dict) -> str:
    return json.dumps({"device_id": int(hotspot_id(a)), "api_key": a["api_key"], "pin": a["pin"],
                       "listen": "0.0.0.0", "port": int(a["port"])}, indent=2) + "\n"


def edit_config_txt(text: str, oled: bool) -> str:
    text = re.sub(rf"\n?{re.escape(MARK_BEGIN)}.*?{re.escape(MARK_END)}\n?", "\n", text, flags=re.S)
    if oled:
        text = re.sub(r"(?m)^#\s*dtparam=i2c_arm=on\s*$", "dtparam=i2c_arm=on", text)
    block = ["[all]", "# GPIO UART for the MMDVM_HS hat; Bluetooth would take it otherwise",
             "enable_uart=1", "dtoverlay=disable-bt"]
    if oled and not re.search(r"(?m)^dtparam=i2c_arm=on\s*$", text):
        block.append("dtparam=i2c_arm=on")
    return text.rstrip("\n") + "\n\n" + "\n".join([MARK_BEGIN, *block, MARK_END]) + "\n"


def edit_cmdline_txt(text: str) -> str:
    # the hat owns the GPIO UART, so no kernel console on it
    words = [w for w in text.split() if not re.fullmatch(r"console=(serial0|ttyAMA0|ttyS0)(,\S*)?", w)]
    return " ".join(words) + "\n"


def find_dmrdash_deb() -> Path | None:
    def ver(p: Path):
        m = re.match(r"dmrdash_([0-9.]+)", p.name)
        return tuple(int(x) for x in m.group(1).split(".") if x) if m else ()
    debs = sorted(PAYLOAD.glob("dmrdash_*_all.deb"), key=ver)
    return debs[-1] if debs else None


# --- the card -------------------------------------------------------------------

def check_boot(path) -> tuple[bool, str]:
    """Is this the boot partition of a freshly flashed 64-bit Pi OS trixie card?"""
    p = Path(path)
    if not (p / "config.txt").is_file() or not (p / "cmdline.txt").is_file():
        return False, "Not a Raspberry Pi boot partition (no config.txt and cmdline.txt)."
    issue = (p / "issue.txt").read_text(errors="replace") if (p / "issue.txt").is_file() else ""
    if "Raspberry Pi" not in issue:
        return False, "Not Raspberry Pi OS (issue.txt doesn't say so)."
    if not (p / "meta-data").is_file():
        return False, "This Pi OS is older than trixie; flash Raspberry Pi OS Lite (64-bit) from 2025-10 or later."
    if (p / "kernel7l.img").is_file() or (p / "kernel.img").is_file():
        return False, "This is the 32-bit Pi OS; DMRHost needs Raspberry Pi OS Lite (64-bit)."
    if "resize" not in (p / "cmdline.txt").read_text(errors="replace").split():
        return False, "This card has already booted. Flash it again with a fresh image first."
    if (p / "dmrhost-setup").exists():
        return True, "Ready (already set up by this installer; writing again replaces it)."
    return True, "Ready: fresh Raspberry Pi OS Lite (64-bit)."


def _mount_points() -> list[Path]:
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(1)  # no "insert a disk" dialogs for empty readers
            mask = ctypes.windll.kernel32.GetLogicalDrives()
        except (AttributeError, OSError):
            mask = (1 << 26) - 1
        return [Path(f"{chr(65 + i)}:\\") for i in range(2, 26) if mask >> i & 1]
    if sys.platform == "darwin":
        return sorted(Path("/Volumes").glob("*"))
    out = []
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2 and parts[2] in ("vfat", "msdos", "exfat"):
                    out.append(Path(parts[1].replace("\\040", " ")))
    except OSError:
        pass
    return out


def find_boot_volumes() -> list[dict]:
    vols = []
    for mp in _mount_points():
        try:
            if not (mp / "cmdline.txt").is_file():
                continue
            ok, msg = check_boot(mp)
        except OSError:
            continue
        vols.append({"path": str(mp), "ok": ok, "status": msg})
    return vols


def plan(a: dict) -> dict:
    """The files written to the boot partition, by path relative to it."""
    files = {
        "user-data": user_data(a),
        "network-config": network_config(a),
        "meta-data": meta_data(),
        "dmrhost-setup/firstboot.sh": (PAYLOAD / "firstboot.sh").read_text(encoding="utf-8"),
        "dmrhost-setup/dmrhost-firstboot.service": (PAYLOAD / "dmrhost-firstboot.service").read_text(encoding="utf-8"),
        "dmrhost-setup/setup.env": setup_env(a),
        "dmrhost-setup/MMDVM.ini": mmdvm_ini(a),
    }
    if a["dashboard"]:
        files["dmrhost-setup/dmrdash.conf"] = dmrdash_conf(a)
    if a["password"]:
        files["dmrhost-setup/login"] = f"{a['username']}:{a['password']}\n"
    return files


def write_card(a: dict, boot) -> list[str]:
    """Write everything to the boot partition at `boot`. Returns what it wrote."""
    boot = Path(boot)
    ok, msg = check_boot(boot)
    if not ok:
        raise ValueError(msg)
    written = []
    setup = boot / "dmrhost-setup"
    if setup.exists():
        for old in setup.iterdir():
            old.unlink()
    setup.mkdir(exist_ok=True)
    for rel, text in plan(a).items():
        # LF line endings even on Windows: the Pi reads these
        (boot / rel).write_bytes(text.encode("utf-8"))
        written.append(rel)
    deb = find_dmrdash_deb() if a["dashboard"] else None
    if deb:
        (setup / deb.name).write_bytes(deb.read_bytes())
        written.append(f"dmrhost-setup/{deb.name}")
    for name, edit in (("config.txt", lambda t: edit_config_txt(t, a["oled"] != "none")),
                       ("cmdline.txt", edit_cmdline_txt)):
        f = boot / name
        f.write_bytes(edit(f.read_bytes().decode("utf-8")).encode("utf-8"))
        written.append(name)
    if hasattr(os, "sync"):
        os.sync()
    return written


def summary(a: dict) -> dict:
    """The review, grouped by wizard step (section id), plus what to set on the radio.
    Shared by both front ends."""
    duplex = a["hat"] == "duplex"
    rx = _mhz(a["rx_freq"]) / 1e6
    tx = (_mhz(a["tx_freq"]) if duplex else _mhz(a["rx_freq"])) / 1e6
    login = " and ".join(x for x in ("SSH key" if a["ssh_keys"] else "", "password" if a["password"] else "") if x)
    groups = [
        {"section": "station", "title": "Station", "rows": [
            ["Hotspot", f"{a['callsign']}, ID {hotspot_id(a)}"],
            ["BrandMeister", f"{a['master_address']}:{a['master_port']}"],
        ]},
        {"section": "radio", "title": "Radio", "rows": [
            ["Hat", "MMDVM_HS duplex" if duplex else "MMDVM_HS simplex"],
            ["Hotspot frequencies", f"RX {rx:.4f} / TX {tx:.4f} MHz" if duplex else f"{rx:.4f} MHz simplex"],
            ["RX offset", f"{int(a['rx_offset'])} Hz"],
            ["Display", {"ssd1306": '0.96" OLED', "sh1106": '1.3" OLED', "none": "none"}[a["oled"]]],
        ]},
        {"section": "pi", "title": "Pi", "rows": [
            ["Login", f"{a['username']}@{a['hostname']}.local, {login}"],
            ["Network", f"Wi-Fi {a['wifi_ssid']} ({a['wifi_country']})" if a["wifi_ssid"] else "Ethernet"],
            ["Time zone", a["timezone"]],
        ]},
    ]
    if a["dashboard"]:
        src = find_dmrdash_deb()
        dash = [["Address", f"http://{a['hostname']}.local:{a['port']}"],
                ["Package", f"{src.name}, on the card" if src else "downloaded on first boot"],
                ["Talkgroup changes", "on (API key given)" if a["api_key"] else "off until you add an API key"]]
    else:
        dash = [["Dashboard", "not installed"]]
    groups.append({"section": "dashboard", "title": "Dashboard", "rows": dash})
    radio = {"tx": f"{rx:.4f}", "rx": f"{tx:.4f}", "cc": str(int(a["color_code"])),
             "slots": "TS1 + TS2" if duplex else "TS2"}
    return {"groups": groups, "radio": radio}
