"""Tests for kea-to-pihole.sh against a fake OPNsense and Pi-hole. Requires bash, curl and jq.
Run: python3 tests/test_kea_to_pihole.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_ftl import FTL, serve, start  # noqa: E402

SCRIPT = os.path.join(HERE, "..", "kea-to-pihole", "kea-to-pihole.sh")


def fake_opnsense(kea):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"rows": kea[self.path[len("/api/"):]]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return serve(Handler)


def bash_path(path):
    """Windows paths for Git Bash; unchanged elsewhere."""
    if os.name != "nt":
        return path
    return subprocess.run(["cygpath", "-u", path], capture_output=True, text=True).stdout.strip()


@unittest.skipUnless(shutil.which("bash") and shutil.which("jq") and shutil.which("curl"), "requires bash, jq and curl")
class KeaTest(unittest.TestCase):
    def setUp(self):
        self.kea = {
            "kea/dhcpv4/search_reservation": [{"ip_address": "192.168.1.20", "hostname": "NAS.old.domain"},
                                              {"ip_address": "192.168.1.5", "hostname": "printer"}],
            "kea/leases4/search": [{"address": "192.168.1.100", "hostname": "Laptop_1"},
                                   {"address": "192.168.1.101", "hostname": "nas"},     # the reservation wins
                                   {"address": "192.168.1.102", "hostname": "manual"},  # the manual record wins
                                   {"address": "192.168.1.103", "hostname": ""}],
            "kea/dhcpv6/search_reservation": [],
            "kea/leases6/search": [{"address": "fd00::20", "hostname": "nas"}],
        }
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conf = os.path.join(self.tmp, "kea-to-pihole.conf")

    def write_conf(self, opn_url, pihole):
        with open(self.conf, "w", newline="\n") as f:
            f.write(f'OPN_URL="{opn_url}"\nOPN_KEY="k"\nOPN_SECRET="s"\nDOMAIN="example.com"\n'
                    f'PIHOLE_URL="{pihole.url}"\nPIHOLE_PASSWORD="{pihole.password}"\n'
                    f'STATE_FILE="{bash_path(self.tmp)}/managed.hosts"\n')

    def run_script(self):
        env = dict(os.environ, KEA_TO_PIHOLE_CONFIG=bash_path(self.conf))
        r = subprocess.run(["bash", bash_path(os.path.abspath(SCRIPT))], env=env,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def check(self, password):
        ph = start(FTL(password))
        ph.config["dns"]["hosts"] = ["192.168.1.2 pihole", "10.0.0.5 manual.example.com manual"]
        self.write_conf(fake_opnsense(self.kea), ph)

        self.run_script()
        self.assertEqual(ph.config["dns"]["hosts"], [
            "192.168.1.2 pihole", "10.0.0.5 manual.example.com manual",
            "192.168.1.5 printer.example.com printer", "192.168.1.20 nas.example.com nas",
            "192.168.1.100 laptop1.example.com laptop1", "fd00::20 nas.example.com nas"])
        self.assertFalse(ph.sessions, "session not logged out")

        # Nothing changed: no PATCH
        ph.calls.clear()
        self.run_script()
        self.assertNotIn(("PATCH", "config"), ph.calls)

        # Laptop gone, printer moved, user added a manual record: only our own lines change
        self.kea["kea/leases4/search"] = self.kea["kea/leases4/search"][1:]
        self.kea["kea/dhcpv4/search_reservation"][1]["ip_address"] = "192.168.1.6"
        ph.config["dns"]["hosts"].append("10.0.0.9 new-manual")
        self.run_script()
        self.assertEqual(ph.config["dns"]["hosts"], [
            "192.168.1.2 pihole", "10.0.0.5 manual.example.com manual", "10.0.0.9 new-manual",
            "192.168.1.6 printer.example.com printer", "192.168.1.20 nas.example.com nas",
            "fd00::20 nas.example.com nas"])

        # A manual IPv6 record for nas replaces Kea's IPv6 record, but not its IPv4 record
        ph.config["dns"]["hosts"].insert(0, "fd00::99 nas.example.com nas")
        self.run_script()
        self.assertIn("192.168.1.20 nas.example.com nas", ph.config["dns"]["hosts"])
        self.assertNotIn("fd00::20 nas.example.com nas", ph.config["dns"]["hosts"])

    def test_with_password(self):
        self.check("app pw")

    def test_without_password(self):
        self.check("")


if __name__ == "__main__":
    unittest.main()
