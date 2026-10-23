import json
import shutil
import subprocess
import sys
import tempfile
import unittest
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


class Shell(unittest.TestCase):
    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_firstboot_parses(self):
        subprocess.run(["sh", "-n", str(hotspot.PAYLOAD / "firstboot.sh")], check=True)


if __name__ == "__main__":
    unittest.main()
