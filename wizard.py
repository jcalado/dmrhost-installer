#!/usr/bin/env python3
"""dmrhost-installer: set up a freshly flashed Raspberry Pi OS card as a DMRHost hotspot.

    python3 wizard.py              opens the wizard in your browser
    python3 wizard.py --cli        asks the questions in the terminal
    python3 wizard.py --cli --answers answers.json [--boot PATH] [--yes]

The browser wizard is served on 127.0.0.1 only, behind a random token.
"""
from __future__ import annotations

import argparse
import getpass
import hmac
import json
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import hotspot

UI = hotspot.resource_dir() / "ui.html"


# --- browser wizard -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    token = ""
    server_version = f"dmrhost-installer/{hotspot.VERSION}"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self):
        # The token is in the URL we open and comes back as a header, so another
        # web page in the same browser can't drive the wizard.
        return hmac.compare_digest(self.headers.get("X-Token", ""), self.token)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, UI.read_bytes(), "text/html; charset=utf-8")
        elif path == "/assets/dmrdash.webp":
            self._send(200, (hotspot.resource_dir() / "assets" / "dmrdash.webp").read_bytes(), "image/webp")
        elif not path.startswith("/api/"):
            self._send(404, {"error": "not found"})
        elif not self._authorised():
            self._send(403, {"error": "bad token"})
        elif path == "/api/form":
            self._send(200, {"sections": hotspot.SECTIONS, "defaults": hotspot.defaults(),
                             "version": hotspot.VERSION,
                             "dmrdash": (hotspot.find_dmrdash_deb() or Path("")).name,
                             "saved": hotspot.load_progress()})
        elif path == "/api/masters":
            self._send(200, hotspot.fetch_masters())
        elif path == "/api/ids":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            self._send(200, hotspot.lookup_ids(q.get("callsign", [""])[0]))
        elif path == "/api/volumes":
            self._send(200, hotspot.find_boot_volumes())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorised():
            return self._send(403, {"error": "bad token"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad JSON"})
        path = self.path.split("?", 1)[0]
        if path == "/api/check":
            a, err = hotspot.validate(body.get("answers", {}))
            only = set(body.get("fields") or hotspot.FIELDS)
            # the summary needs every answer, not just this step's
            summary = [] if err else hotspot.summary(a)
            self._send(200, {"errors": {k: v for k, v in err.items() if k in only}, "summary": summary})
        elif path == "/api/write":
            a, err = hotspot.validate(body.get("answers", {}))
            if err:
                return self._send(200, {"errors": err})
            try:
                written = hotspot.write_card(a, body.get("boot", ""))
            except (ValueError, OSError) as e:
                return self._send(200, {"error": str(e)})
            self._send(200, {"written": written})
        elif path == "/api/progress":
            try:
                if body.get("clear"):
                    hotspot.clear_progress()
                else:
                    hotspot.save_progress(body.get("answers") or {}, int(body.get("step") or 0))
            except (OSError, ValueError, TypeError) as e:
                return self._send(200, {"error": str(e)})
            self._send(200, {})
        elif path == "/api/quit":
            self._send(200, {})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, {"error": "not found"})


def run_gui(port: int, open_browser: bool) -> None:
    Handler.token = secrets.token_urlsafe(18)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/#{Handler.token}"
    # flush: a bundled .exe ignores PYTHONUNBUFFERED, and a piped console would hide the address
    print(f"dmrhost-installer {hotspot.VERSION}: the wizard is at\n  {url}\nPress Ctrl+C here to quit.", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# --- command line ---------------------------------------------------------------

def ask_field(f: dict, current, answers: dict | None = None) -> object:
    label = f["label"]
    if f.get("warn"):
        print(f"  ! {f['warn']}" + (f" {f['link']}" if f.get("link") else ""))
    if f.get("help"):
        print(f"  {f['help']}")
    if f.get("find"):
        link = hotspot.find_link(f, answers or {})
        print(f"  {f['find']}" + (f"\n  {link}" if link else ""))
    if f["kind"] == "bool":
        d = "Y/n" if current else "y/N"
        r = input(f"{label}? [{d}] ").strip().lower()
        return current if r == "" else r in ("y", "yes")
    if f["kind"] == "choice":
        for i, (val, name, desc) in enumerate(f["choices"], 1):
            print(f"  {i}. {name}{' - ' + desc if desc else ''}{'  (default)' if val == current else ''}")
        r = input(f"{label} [1-{len(f['choices'])}]: ").strip()
        return f["choices"][int(r) - 1][0] if r.isdigit() and 1 <= int(r) <= len(f["choices"]) else current
    if f["kind"] == "password":
        shown = " [keep]" if current else ""
        r = getpass.getpass(f"{label}{shown}: ")
        return r or current
    if f["kind"] == "master":
        masters = hotspot.fetch_masters()
        if masters:
            for cc in sorted({m["country"] for m in masters}):
                print(f"  {cc}: " + ", ".join(str(m["id"]) for m in masters if m["country"] == cc))
        else:
            print("  (couldn't fetch the master list; type the address)")
    if f["kind"] == "textarea" and current:
        print(f"  Current: {str(current)[:60]}{'...' if len(str(current)) > 60 else ''}")
        r = input(f"{label} [Enter keeps it, '-' clears]: ").strip()
        return "" if r == "-" else (r or current)
    shown = f" [{current}]" if current not in ("", None) else ""
    r = input(f"{label}{shown}: ").strip()
    return r or current


def suggest_ids(answers: dict) -> None:
    hits = hotspot.lookup_ids(answers.get("callsign", ""))
    if not hits:
        return
    print("  On radioid.net:")
    for h in hits:
        where = ", ".join(x for x in (h["city"], h["country"]) if x)
        print(f"    {h['id']}  {h['name']}{', ' + where if where else ''}")
    if not answers.get("dmr_id"):
        answers["dmr_id"] = str(hits[0]["id"])


def run_cli(answers_file: str | None, boot: str | None, yes: bool) -> int:
    answers = hotspot.defaults()
    if answers_file:
        answers.update(json.loads(Path(answers_file).read_text(encoding="utf-8")))
    interactive = not (answers_file and yes)
    while True:
        if interactive:
            for s in hotspot.SECTIONS:
                print(f"\n== {s['title']}")
                for f in s["fields"]:
                    if hotspot.visible(f, answers):
                        answers[f["key"]] = ask_field(f, answers.get(f["key"], ""), answers)
                        if f["key"] == "callsign":
                            suggest_ids(answers)
        a, err = hotspot.validate(answers)
        if not err:
            break
        print("\nPlease fix:")
        for k, msg in err.items():
            print(f"  {hotspot.FIELDS[k]['label']}: {msg}")
        if not interactive:
            return 2
        input("Press Enter to go through the questions again. ")

    print()
    review = hotspot.summary(a)
    for g in review["groups"]:
        print(f"  {g['title']}")
        for k, v in g["rows"]:
            print(f"    {k:20} {v}")
    r = review["radio"]
    print(f"\n  Set your radio to TX {r['tx']} / RX {r['rx']} MHz, colour code {r['cc']}, {r['slots']}")

    if not boot:
        vols = hotspot.find_boot_volumes()
        print("\nCards found:" if vols else "\nNo Raspberry Pi boot partition is mounted.")
        for i, v in enumerate(vols, 1):
            print(f"  {i}. {v['path']}  {v['status']}")
        if yes and len([v for v in vols if v["ok"]]) == 1:
            boot = next(v["path"] for v in vols if v["ok"])
        elif interactive:
            r = input("Card to write (number, or a path to its boot partition): ").strip()
            boot = vols[int(r) - 1]["path"] if r.isdigit() and 1 <= int(r) <= len(vols) else r
        if not boot:
            print("Flash Raspberry Pi OS Lite (64-bit) with Raspberry Pi Imager, skip its OS customisation,\n"
                  "then plug the card back in and run this again.")
            return 1
    ok, msg = hotspot.check_boot(boot)
    print(f"{boot}: {msg}")
    if not ok:
        return 1
    if not yes and input("Write the hotspot setup to this card? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    for rel in hotspot.write_card(a, boot):
        print(f"  wrote {rel}")
    print(f"\nDone. Eject the card, put it in the Pi and power it up. Setup takes 5-15 minutes;\n"
          f"then the hotspot logs in to BrandMeister"
          + (f" and the dashboard is at http://{a['hostname']}.local:{a['port']}" if a["dashboard"] else "")
          + f".\nProgress: ssh {a['username']}@{a['hostname']}.local tail -f /var/log/dmrhost-firstboot.log")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Set up a Raspberry Pi OS card as a DMRHost hotspot.")
    p.add_argument("--cli", action="store_true", help="ask in the terminal instead of the browser")
    p.add_argument("--answers", help="JSON file of answers (keys as in hotspot.SECTIONS)")
    p.add_argument("--boot", help="path of the card's mounted boot partition")
    p.add_argument("--yes", action="store_true", help="don't ask; with --answers, fully unattended")
    p.add_argument("--port", type=int, default=0, help="port for the browser wizard (default: any free one)")
    p.add_argument("--no-browser", action="store_true", help="print the wizard's address, don't open it")
    p.add_argument("--version", action="version", version=hotspot.VERSION)
    p.add_argument("--check-tls", action="store_true", help="check that the BrandMeister master list downloads")
    args = p.parse_args()
    if args.check_tls:
        n = len(hotspot.fetch_masters())
        print(f"{n} BrandMeister masters")
        return 0 if n else 1
    if args.cli or args.answers:
        try:
            return run_cli(args.answers, args.boot, args.yes)
        except (KeyboardInterrupt, EOFError):
            print()
            return 130
    run_gui(args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
