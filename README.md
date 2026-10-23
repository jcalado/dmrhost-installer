# dmrhost-installer

Turns a freshly flashed Raspberry Pi OS card into a [BrandMeister DMRHost](https://github.com/BrandMeister/DMRHost)
hotspot for an MMDVM_HS GPIO hat, with the [dmrdash](https://github.com/jcalado/dmrdash) dashboard.
You answer the questions once, on your computer, and the Pi sets itself up on its first boot.

![The wizard's Radio step](docs/wizard-radio.png)

> **Status: early.** The wizard and the files it writes are tested against the stock image's boot files, and the
> generated `MMDVM.ini` matches a working hotspot's. The full flow (card, first boot, live hotspot) has not yet been
> run on real hardware.

## Requirements

**On the Pi side**

- A Raspberry Pi Zero 2 W, 3 or 4 (64-bit). The Pi 5 is not supported yet: its GPIO UART isn't `/dev/ttyAMA0`.
- An MMDVM_HS GPIO hat, duplex or simplex, optionally with a 0.96" (SSD1306) or 1.3" (SH1106) OLED.
- An SD card, 8 GB or more.
- Internet access on first boot, over Wi-Fi or Ethernet (a Pi Zero has no Ethernet).
- A BrandMeister account with a hotspot security password set in [SelfCare](https://brandmeister.network/#/selfcare).
  This is not your BrandMeister login password. For talkgroup changes from the
  dashboard, also a SelfCare API key: on brandmeister.network, your callsign (top right) → Profile settings →
  Security settings → API keys, or `https://brandmeister.network/#/profile/<CALLSIGN>/api`.

**On your computer**

- Linux, macOS or Windows with Python 3.9 or newer. Nothing else to install.
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/), to flash the card.

## Use

1. Flash **Raspberry Pi OS Lite (64-bit)**, trixie (2025-10) or newer, with
   [Raspberry Pi Imager](https://www.raspberrypi.com/software/). Either pick it in Imager under
   **Raspberry Pi OS (other)**, or [download the latest image](https://downloads.raspberrypi.com/raspios_lite_arm64_latest)
   and choose **Use custom**. If Imager offers to customise the OS, skip it: the installer sets the user, Wi-Fi and
   hostname itself. The wizard's Start page has the same links.
2. Take the card out and put it back in, so your computer mounts its `bootfs` partition.
3. Run the wizard:

   ```sh
   python3 wizard.py            # opens in your browser
   python3 wizard.py --cli      # same questions in the terminal
   ```

   On Windows, use `py wizard.py` if `python3` isn't found. You can fill in the answers while Imager is still writing.
   Type your callsign and the wizard looks up your DMR IDs on radioid.net. Picking one also picks the BrandMeister
   master it last used.
4. Pick the card and write. Eject it, put it in the Pi with the hat on, and power it up.
5. The first boot takes 5 to 15 minutes. Then the hotspot logs in to BrandMeister, the OLED lights up, and the
   dashboard is at `http://<hostname>.local:8080`.

### Unattended

Copy [`answers.example.json`](answers.example.json), fill it in, and run:

```sh
python3 wizard.py --answers answers.json --boot /media/you/bootfs --yes
```

Leave out `--boot` and `--yes` picks the card itself if exactly one is mounted. Leave out `--yes` and it asks for
anything missing and confirms before writing. A few notes on the answers:

- `master` is a BrandMeister master ID from <https://api.brandmeister.network/v2/master/>, or an address with an
  optional `:port` (62031 by default).
- `rx_freq` and `tx_freq` are the **hotspot's** frequencies in MHz: your radio transmits on `rx_freq`. A simplex hat
  ignores `tx_freq`.
- `oled` is `ssd1306`, `sh1106` or `none`. `hat` is `duplex` or `simplex`.
- Give `ssh_key`, `password`, or both. Leave `wifi_ssid` blank for Ethernet.

## What it does

The wizard only writes to the card's FAT boot partition, which every OS mounts by itself, so it needs no admin
rights and no raw disk access. It refuses a card that has already booted, the 32-bit OS, or one older than trixie.

| File | What for |
|---|---|
| `user-data` | cloud-init: hostname, time zone, your user and SSH key; starts the setup |
| `network-config` | Wi-Fi (or Ethernet) and the Wi-Fi country |
| `meta-data` | a fresh cloud-init instance ID |
| `config.txt` | `enable_uart=1` and `dtoverlay=disable-bt` (the hat uses the GPIO UART), and I2C for the OLED |
| `cmdline.txt` | removes the serial console from the hat's UART |
| `dmrhost-setup/` | the first-boot script and service, `MMDVM.ini`, `dmrdash.conf`, and the dmrdash package |

On the first boot, cloud-init runs `firstboot.sh --stage`, which moves `dmrhost-setup/` to `/var/lib/dmrhost-setup`
(root only), deletes it from the boot partition, and blanks `network-config`. Then `dmrhost-firstboot.service`:

- adds the DMRHost apt repo (`repo.test.net.in/dmrhost`) and installs `dmrhost`, plus `displayserver` for an OLED
- installs `/etc/MMDVM.ini` (the packaged one is kept as `/etc/MMDVM.ini.dist`)
- adds a weekly radioid.net download to `/usr/local/etc/user.csv`, trimmed to ID, callsign and name to save RAM
- loads `i2c-dev` before DisplayServer starts (without it the OLED stays blank)
- installs dmrdash with `/etc/dmrdash.conf`
- sets your login password, if you chose one
- deletes the copied secrets, marks itself done and disables itself

If it fails, for example because the Pi isn't online yet, it tries again a minute later, and keeps trying across
reboots until it succeeds.

## Security

- Until the first boot, the card's boot partition holds your BrandMeister hotspot password, Wi-Fi password,
  login password, API key and dashboard PIN in plain text, readable by anyone with the card. The first boot moves
  them to root-only files and deletes them from the card. Keep the card to yourself until then.
- The login user gets passwordless `sudo`. Password logins over SSH are only enabled if you set a password.
- The browser wizard listens on `127.0.0.1` only, on a random port. Every request needs a random token that is
  only in the address the wizard opens, so other websites in your browser can't use it.
- The dashboard listens on your whole LAN. Set a PIN if others share your network.
- The wizard saves your answers as you go, so closing the browser loses nothing. They go to
  `~/.config/dmrhost-installer/progress.json` (macOS: `~/Library/Application Support/…`, Windows: `%APPDATA%\…`),
  readable only by you. Passwords, the API key and the PIN are never saved there. **Start over** on the first page
  deletes the file.

## Troubleshooting

Start with the first-boot log:

```sh
ssh <user>@<hostname>.local
tail -f /var/log/dmrhost-firstboot.log
systemctl status dmrhost-firstboot dmrhost
```

- **The Pi never shows up on the network.** Check the Wi-Fi name, password and country, and that it's a 2.4 GHz
  network: a Pi Zero 2 W has no 5 GHz. If `<hostname>.local` doesn't resolve, find the Pi's address in your router.
  To start over, flash the card again and rerun the wizard: it refuses a card that has already booted.
- **The log says "no network after 10 minutes, will retry".** The Pi is up but can't reach the internet. It retries
  every minute; `systemctl restart dmrhost-firstboot` tries straight away.
- **The OLED stays blank.** Check that `lsmod | grep i2c_dev` shows the module and that `i2cdetect -y 1` finds the
  display at `3c`. Try the other display type: change `Type` under `[OLED]` in `/etc/MMDVM.ini` (3 = SSD1306,
  6 = SH1106), then `sudo systemctl restart displayserver`.
- **The hotspot doesn't log in to BrandMeister.** `journalctl -u dmrhost -f` shows the login. A rejected login
  usually means the hotspot security password doesn't match the one in SelfCare. Fix `Password` under
  `[DMR Network]` in `/etc/MMDVM.ini` and restart `dmrhost`.
- **The radio doesn't get through, or the BER is high.** Check that the radio transmits on the hotspot's RX
  frequency, then tune the RX offset from the dashboard. The hat's oscillator drifts with temperature.
- **There's no dashboard.** The log says whether dmrdash was installed. If it was skipped, install it later with
  dmrdash's `install.sh`.

## dmrdash

`./fetch-dmrdash.sh` puts the latest dmrdash release in `payload/` (it needs `gh` while that repo is private), and
the wizard copies it onto the card. Without it, the Pi tries to download the package from GitHub on first boot, and
skips the dashboard if it can't.

## Develop

```sh
python3 -m unittest discover -s tests -v
shellcheck payload/firstboot.sh fetch-dmrdash.sh
```

- `hotspot.py` holds the questions (`SECTIONS`), validation and rendering, shared by both front ends.
- `wizard.py` holds the browser server and the command line. `ui.html` is the browser wizard: one file, no build step.
- `payload/` is what goes onto the card. `MMDVM.ini.in` started as a working CT7BLE hotspot's config.
- `tests/fixtures/bootfs` holds the boot files of the stock 2026-10-06 trixie Lite image.

## Roadmap

- A test on real hardware, from a blank card to a live hotspot.
- Windows, macOS and Linux binaries built by PyInstaller in CI, so you don't need Python.
- Flashing the card from the wizard too, probably through Raspberry Pi Imager's command line.
- A pre-built image with the packages already installed, for offline setup. It would read the same
  `dmrhost-setup/` files.
- Pi 5 support.

## Licence

MIT. See [LICENSE](LICENSE).
