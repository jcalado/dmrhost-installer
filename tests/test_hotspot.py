import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hotspot  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bootfs"  # from raspios trixie lite 2026-10-06
MASTERS = [{"id": 2682, "country": "PT", "address": "193.137.237.12"}]
GOOD = {
    "callsign": "ct7ble", "dmr_id": "2680513", "id_suffix": "01", "master": "2682",
    "bm_password": "s3cret$pw", "location": "Portugal", "url": "",
    "hat": "duplex", "rx_freq": "432.000", "tx_freq": "434,000", "rx_offset": "-350", "color_code": "1",
    "oled": "ssd1306", "hostname": "dmrhost", "username": "joel",
    "ssh_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFRLzw8x5xbX4yJ joel@example",
    "password": "", "timezone": "Europe/Lisbon",
    "wifi_ssid": "Home \"Wi-Fi\"", "wifi_password": "pass: word#1", "wifi_country": "pt",
    "dashboard": True, "api_key": "KEY", "pin": "1234", "port": "8080",
}


def answers(**over):
    a, err = hotspot.validate({**GOOD, **over}, MASTERS)
    return a, err


def ini(text):
    out, sec = {}, None
    for ln in text.splitlines():
        if ln.startswith("["):
            sec = ln.strip("[]")
        elif "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            out[(sec, k)] = v
    return out


class Validate(unittest.TestCase):
    def test_good(self):
        a, err = answers()
        self.assertEqual(err, {})
        self.assertEqual(a["callsign"], "CT7BLE")
        self.assertEqual(a["wifi_country"], "PT")
        self.assertEqual((a["master_address"], a["master_port"]), ("193.137.237.12", 62031))

    def test_errors(self):
        _, err = answers(dmr_id="123", rx_freq="abc", id_suffix="00", hostname="Bad_Host",
                         master="9999", pin="1 2", wifi_country="")
        for k in ("dmr_id", "rx_freq", "id_suffix", "hostname", "master", "pin", "wifi_country"):
            self.assertIn(k, err)

    def test_duplex_needs_two_frequencies(self):
        self.assertIn("tx_freq", answers(tx_freq="432.0")[1])
        self.assertNotIn("tx_freq", answers(hat="simplex", tx_freq="")[1])

    def test_login_needed(self):
        self.assertIn("password", answers(ssh_key="", password="")[1])
        self.assertEqual(answers(ssh_key="", password="longenough")[1], {})

    def test_no_newlines_in_ini_values(self):
        self.assertIn("location", answers(location="a\nCallsign=EVIL")[1])

    def test_hidden_fields_not_checked(self):
        _, err = answers(dashboard=False, pin="x", port="nope")
        self.assertEqual(err, {})

    def test_find_link(self):
        f = hotspot.FIELDS["api_key"]
        self.assertEqual(hotspot.find_link(f, {"callsign": "ct7ble"}), "https://brandmeister.network/#/profile/CT7BLE/api")
        self.assertEqual(hotspot.find_link(f, {"callsign": "a/../b"}), "https://brandmeister.network/#/profile/AB/api")
        self.assertEqual(hotspot.find_link(f, {}), "")

    def test_typed_master(self):
        a, err = answers(master="example.net:62032")
        self.assertEqual(err, {})
        self.assertEqual((a["master_address"], a["master_port"]), ("example.net", 62032))


class Render(unittest.TestCase):
    def test_mmdvm_ini_duplex(self):
        c = ini(hotspot.mmdvm_ini(answers()[0]))
        self.assertEqual(c[("General", "Id")], "268051301")
        self.assertEqual(c[("General", "Duplex")], "1")
        self.assertEqual(c[("Info", "RXFrequency")], "432000000")
        self.assertEqual(c[("Info", "TXFrequency")], "434000000")
        self.assertEqual(c[("Modem", "RXOffset")], "-350")
        self.assertEqual(c[("DMR Network", "Password")], "s3cret$pw")
        self.assertEqual(c[("DMR Network", "Slot1")], "1")
        self.assertEqual(c[("Info", "URL")], "https://www.qrz.com/db/CT7BLE")
        self.assertEqual(c[("OLED", "Type")], "3")
        self.assertEqual(c[("OLED", "Scroll")], "0")
        self.assertEqual(c[("Display", "Enable")], "1")

    def test_mmdvm_ini_simplex_no_display(self):
        c = ini(hotspot.mmdvm_ini(answers(hat="simplex", tx_freq="", oled="none")[0]))
        self.assertEqual(c[("General", "Duplex")], "0")
        self.assertEqual(c[("Info", "TXFrequency")], "432000000")
        self.assertEqual(c[("DMR Network", "Slot1")], "0")
        self.assertEqual(c[("Display", "Enable")], "0")

    def test_yaml(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed")
        a = answers(password="longenough")[0]
        ud = yaml.safe_load(hotspot.user_data(a))
        self.assertEqual(ud["users"][0]["name"], "joel")
        self.assertEqual(ud["users"][0]["ssh_authorized_keys"], [GOOD["ssh_key"]])
        self.assertTrue(ud["ssh_pwauth"])
        self.assertIn("--stage", ud["runcmd"][-1])
        net = yaml.safe_load(hotspot.network_config(a))["network"]
        self.assertEqual(net["wifis"]["wlan0"]["access-points"], {'Home "Wi-Fi"': {"password": "pass: word#1"}})
        self.assertEqual(net["wifis"]["wlan0"]["regulatory-domain"], "PT")
        open_net = yaml.safe_load(hotspot.network_config(answers(wifi_password="")[0]))["network"]
        self.assertEqual(open_net["wifis"]["wlan0"]["access-points"], {'Home "Wi-Fi"': {}})
        wired = yaml.safe_load(hotspot.network_config(answers(wifi_ssid="")[0]))["network"]
        self.assertNotIn("wifis", wired)
        self.assertFalse(wired["ethernets"]["eth0"]["optional"])

    def test_dmrdash_conf(self):
        c = json.loads(hotspot.dmrdash_conf(answers()[0]))
        self.assertEqual(c, {"device_id": 268051301, "api_key": "KEY", "pin": "1234", "listen": "0.0.0.0", "port": 8080})

    def test_config_txt(self):
        stock = (FIXTURE / "config.txt").read_text()
        once = hotspot.edit_config_txt(stock, oled=True)
        self.assertIn("\ndtparam=i2c_arm=on\n", once)
        self.assertNotIn("#dtparam=i2c_arm=on", once)
        self.assertIn("enable_uart=1", once)
        self.assertIn("dtoverlay=disable-bt", once)
        self.assertEqual(hotspot.edit_config_txt(once, oled=True), once)
        self.assertEqual(once.count("enable_uart=1"), 1)

    def test_cmdline_txt(self):
        out = hotspot.edit_cmdline_txt((FIXTURE / "cmdline.txt").read_text())
        self.assertNotIn("serial0", out)
        self.assertIn("console=tty1", out)
        self.assertIn(" resize", out)
        self.assertEqual(out.count("\n"), 1)


class Card(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.boot = self.tmp / "bootfs"
        shutil.copytree(FIXTURE, self.boot)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_write(self):
        a = answers(password="longenough")[0]
        written = hotspot.write_card(a, self.boot)
        for rel in ("user-data", "network-config", "meta-data", "config.txt", "cmdline.txt",
                    "dmrhost-setup/firstboot.sh", "dmrhost-setup/MMDVM.ini", "dmrhost-setup/setup.env",
                    "dmrhost-setup/dmrdash.conf", "dmrhost-setup/login"):
            self.assertIn(rel, written)
            self.assertTrue((self.boot / rel).is_file(), rel)
        self.assertNotIn(b"\r\n", (self.boot / "dmrhost-setup/firstboot.sh").read_bytes())
        self.assertEqual((self.boot / "dmrhost-setup/login").read_text(), "joel:longenough\n")
        env = (self.boot / "dmrhost-setup/setup.env").read_text()
        self.assertIn("OLED=1\n", env)
        self.assertIn("DASHBOARD=1\n", env)
        # writing again (say, after fixing an answer) replaces the old setup
        a2 = answers(dashboard=False)[0]
        hotspot.write_card(a2, self.boot)
        self.assertFalse((self.boot / "dmrhost-setup/login").exists())
        self.assertFalse((self.boot / "dmrhost-setup/dmrdash.conf").exists())
        self.assertEqual((self.boot / "config.txt").read_text().count(hotspot.MARK_BEGIN), 1)

    def test_refuses_booted_card(self):
        (self.boot / "cmdline.txt").write_text("console=tty1 root=PARTUUID=x rootwait\n")
        self.assertFalse(hotspot.check_boot(self.boot)[0])
        with self.assertRaises(ValueError):
            hotspot.write_card(answers()[0], self.boot)

    def test_refuses_old_or_32bit(self):
        (self.boot / "kernel7l.img").touch()
        self.assertIn("32-bit", hotspot.check_boot(self.boot)[1])
        (self.boot / "kernel7l.img").unlink()
        (self.boot / "meta-data").unlink()
        self.assertIn("older than trixie", hotspot.check_boot(self.boot)[1])

    def test_ok(self):
        self.assertTrue(hotspot.check_boot(self.boot)[0])


class Progress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(hotspot, "progress_path", return_value=self.tmp / "d" / "progress.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp)

    def test_round_trip_without_secrets(self):
        hotspot.save_progress({**GOOD, "password": "longenough", "junk": "x"}, 3)
        saved = hotspot.load_progress()
        self.assertEqual(saved["step"], 3)
        self.assertEqual(saved["answers"]["callsign"], "ct7ble")
        self.assertIs(saved["answers"]["dashboard"], True)
        raw = hotspot.progress_path().read_text()
        for secret in ("s3cret$pw", "pass: word#1", "longenough", "KEY", "1234\""):
            self.assertNotIn(secret, raw)
        self.assertNotIn("junk", saved["answers"])
        if not sys.platform.startswith("win"):
            self.assertEqual(hotspot.progress_path().stat().st_mode & 0o777, 0o600)

    def test_step_is_clamped(self):
        hotspot.save_progress({}, 99)
        self.assertEqual(hotspot.load_progress()["step"], len(hotspot.SECTIONS))

    def test_missing_or_broken(self):
        self.assertIsNone(hotspot.load_progress())
        hotspot.progress_path().parent.mkdir(parents=True)
        hotspot.progress_path().write_text("{not json")
        self.assertIsNone(hotspot.load_progress())
        hotspot.clear_progress()
        hotspot.clear_progress()  # twice is fine
        self.assertFalse(hotspot.progress_path().exists())


class RadioID(unittest.TestCase):
    def setUp(self):
        hotspot._ids_cache.clear()

    def fake(self, rows):
        body = json.dumps({"count": len(rows), "results": rows}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(body)
        return mock.patch("urllib.request.urlopen", return_value=resp)

    def test_newest_first(self):
        rows = [
            {"id": 2680810, "callsign": "CT7BLE", "fname": "Joel", "surname": "", "city": "Povoa", "country": "Portugal",
             "lastheard": "Sun, 19 Jul 2026 14:06:33 GMT", "lastmaster": "2682"},
            {"id": 2680513, "callsign": "CT7BLE", "fname": "Joel", "surname": "", "city": "Povoa", "country": "Portugal",
             "lastheard": "Fri, 23 Oct 2026 08:53:09 GMT", "lastmaster": 2682},
            {"id": 1234567, "callsign": "OTHER", "lastheard": ""},
        ]
        with self.fake(rows) as u:
            ids = hotspot.lookup_ids("ct7ble")
            self.assertIn("callsign=CT7BLE", u.call_args[0][0].full_url)
        self.assertEqual([r["id"] for r in ids], [2680513, 2680810])
        self.assertEqual(ids[0]["lastmaster"], "2682")
        self.assertEqual(ids[0]["name"], "Joel")

    def test_bad_callsign_never_asks(self):
        with mock.patch("urllib.request.urlopen") as u:
            self.assertEqual(hotspot.lookup_ids("a b&c=1"), [])
            u.assert_not_called()

    def test_offline(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertEqual(hotspot.lookup_ids("CT7BLE"), [])


def backup_zip(files, folder=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(folder + name, text)
    return buf.getvalue()


# trimmed from a Pi-Star / WPSD backup: MMDVMHost talks to BrandMeister through DMRGateway
BACKUP = {
    "mmdvmhost": "[General]\nCallsign=ct7ble\nId=268051302\nDuplex=1\nDisplay=OLED\n\n"
                 "[Info]\nRXFrequency=438800000\nTXFrequency=431200000\nLocation=Povoa, PT\nURL=https://www.qrz.com/db/CT7BLE\n\n"
                 "[Modem]\nRXOffset=-275\n\n[DMR]\nEnable=1\nColorCode=2\n\n"
                 "[DMR Network]\nEnable=1\nAddress=127.0.0.1\nPort=62031\nPassword=none\n\n[OLED]\nType=6\n",
    "dmrgateway": "[DMR Network 1]\nEnabled=1\nName=BM_2682_Portugal\nAddress=193.137.237.12\nPort=62031\n"
                  "Password=\"s3cret$pw\"\n\n[DMR Network 2]\nEnabled=1\nName=DMR+_IPSC2\nAddress=1.2.3.4\nPassword=PASSWORD\n",
    "wpa_supplicant.conf": "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\ncountry=pt\n\n"
                           "network={\n\tssid=\"Cafe\"\n\tpsk=\"latte1234\"\n}\n\n"
                           "network={\n\tssid=\"Home Wi-Fi\"\n\tpsk=" + "ab" * 32 + "\n\tpriority=5\n}\n",
    "hostname": "pi-star\n",
    "bmapi.key": "[key]\napikey=APIKEY123\n",
    "config.php": "<?php\ndate_default_timezone_set('Europe/Lisbon');\n",
    "ysfgateway": "[General]\nCallsign=CT7BLE\n",
}


class ImportBackup(unittest.TestCase):
    def test_wpsd_backup(self):
        r = hotspot.read_backup(backup_zip(BACKUP), MASTERS)
        a = r["answers"]
        self.assertEqual(a["callsign"], "CT7BLE")
        self.assertEqual((a["dmr_id"], a["id_suffix"]), ("2680513", "02"))
        self.assertEqual((a["hat"], a["rx_freq"], a["tx_freq"]), ("duplex", "438.800", "431.200"))
        self.assertEqual((a["rx_offset"], a["color_code"], a["oled"]), ("-275", "2", "sh1106"))
        self.assertEqual((a["master"], a["bm_password"]), ("2682", "s3cret$pw"))
        self.assertEqual((a["hostname"], a["timezone"], a["api_key"]), ("pi-star", "Europe/Lisbon", "APIKEY123"))
        # the highest priority network wins, and its hashed key is kept as is
        self.assertEqual((a["wifi_ssid"], a["wifi_password"], a["wifi_country"]), ("Home Wi-Fi", "ab" * 32, "PT"))
        self.assertTrue(any("2 Wi-Fi networks" in n for n in r["notes"]))
        # with a login added, what came out of the backup passes as it is
        _, err = hotspot.validate({**hotspot.defaults(), **a, "username": "joel", "password": "longenough"}, MASTERS)
        self.assertEqual(err, {})

    def test_direct_master_and_simplex(self):
        mm = ("[General]\nCallsign=CT7BLE\nId=2680513\nDuplex=0\nDisplay=None\n[Info]\nRXFrequency=433412500\n"
              "TXFrequency=433412500\n[DMR Network]\nAddress=bm.example.org\nPort=55555\nPassword=pw\n")
        r = hotspot.read_backup(backup_zip({"mmdvmhost": mm}, folder="WPSD_Config/"), MASTERS)
        a = r["answers"]
        self.assertEqual((a["dmr_id"], a["hat"], a["rx_freq"], a["oled"]), ("2680513", "simplex", "433.4125", "none"))
        self.assertNotIn("tx_freq", a)
        self.assertNotIn("id_suffix", a)
        self.assertEqual(a["master"], "bm.example.org:55555")
        self.assertTrue(any("268051301" in n for n in r["notes"]))

    def test_not_a_backup(self):
        with self.assertRaises(ValueError):
            hotspot.read_backup(b"not a zip")
        with self.assertRaises(ValueError):
            hotspot.read_backup(backup_zip({"hosts": "127.0.0.1 localhost\n"}))


class Shell(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_firstboot_parses(self):
        subprocess.run(["sh", "-n", str(hotspot.PAYLOAD / "firstboot.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
