"""Tests for pihole-sync.py against fake Pi-holes. Run: python3 tests/test_pihole_sync.py"""
import importlib.util
import logging
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from mock_ftl import FTL, start  # noqa: E402

spec = importlib.util.spec_from_file_location("ps", os.path.join(HERE, "..", "pihole-sync.py"))
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)
ps.time.sleep = lambda s: None  # no waiting between retries


class Args:
    dry_run = False
    once = True


def config(primary, *replicas, dry_run=False, **env):
    os.environ.clear()
    os.environ.update(PRIMARY=primary, REPLICAS=",".join(replicas), SYNC="lists,domains,clients,config,blocking", **env)
    args = Args()
    args.dry_run = dry_run
    return ps.load_config(args)


def endpoint(ftl):
    return f"{ftl.url}|{ftl.password}" if ftl.password else ftl.url


def state(ftl):
    """The replica-independent view of a fake Pi-hole, for comparing primary and replica."""
    class Fake:
        def get(self, path, key):
            return {"groups": ftl.groups, "lists": ftl.lists, "domains": ftl.domains,
                    "clients": ftl.clients, "config": ftl.config}[key]

        def request(self, *a):
            return ftl.blocking
    st = ps.read_state(Fake(), ps.ALL_SYNC)
    st["config"] = {k: ps.get_path(ftl.config, k) for k in ps.DEFAULT_CONFIG_KEYS}
    return st


def populated_pair():
    prim = start(FTL("pw,1|x", 0))
    rep = start(FTL("rpw", 500))
    # Primary: groups with IDs that differ from the replica, adlists, regex, punycode, clients, settings
    prim.groups.append({"id": 5, "name": "Kids", "comment": None, "enabled": True})
    prim.groups.append({"id": 6, "name": "IoT", "comment": "gadgets", "enabled": False})
    prim.lists.append({"id": 1, "address": "https://example.com/list.txt?a=1&b=2", "type": "block",
                       "comment": "", "groups": [0, 5], "enabled": True})
    prim.lists.append({"id": 2, "address": "https://allow.example/x", "type": "allow",
                       "comment": "a", "groups": [0], "enabled": False})
    prim.domains.append({"id": 1, "domain": r"(\.|^)tiktok\.com$", "type": "deny", "kind": "regex",
                         "comment": None, "groups": [5], "enabled": True})
    prim.domains.append({"id": 2, "domain": "xn--bcher-kva.example", "type": "allow", "kind": "exact",
                         "comment": None, "groups": [0], "enabled": True})
    prim.clients.append({"id": 1, "client": "192.168.1.0/24", "comment": "lan", "groups": [0, 6]})
    prim.config["dns"]["hosts"] = ["192.168.1.10 nas.lan"]
    prim.config["dns"]["upstreams"] = ["9.9.9.9", "149.112.112.112"]
    prim.config["dns"]["interface"] = "eth1"  # host-specific - must not be synced
    prim.blocking = {"blocking": "disabled", "timer": 300}
    # Replica: old leftovers that must go, and a group with the same name but another ID
    rep.groups.append({"id": 900, "name": "IoT", "comment": "old", "enabled": True})
    rep.groups.append({"id": 901, "name": "Old", "comment": None, "enabled": True})
    rep.lists.append({"id": 9, "address": "https://old.example/list", "type": "block",
                      "comment": None, "groups": [901], "enabled": True})
    rep.domains.append({"id": 9, "domain": "ads.example", "type": "deny", "kind": "exact",
                        "comment": None, "groups": [0], "enabled": True})
    return prim, rep


class SyncTest(unittest.TestCase):
    def test_full_sync(self):
        prim, rep = populated_pair()
        cfg = config(endpoint(prim), endpoint(rep))

        self.assertTrue(ps.run_once(cfg))
        self.assertEqual(state(rep), state(prim))
        self.assertEqual(rep.config["dns"]["interface"], "eth0", "host-specific setting was synced")
        self.assertIn(("POST", "action/gravity"), rep.calls)
        self.assertEqual(prim.writes(), [], "wrote to the primary")
        self.assertFalse(prim.sessions or rep.sessions, "sessions not logged out")

        # Second run: nothing changed, so nothing is written
        rep.calls.clear()
        self.assertTrue(ps.run_once(cfg))
        self.assertEqual(rep.writes(), [])

        # Only a comment changed: synced, but no gravity
        prim.lists[0]["comment"] = "new"
        rep.calls.clear()
        self.assertTrue(ps.run_once(cfg))
        self.assertNotIn(("POST", "action/gravity"), rep.calls)
        self.assertEqual(state(rep), state(prim))

    def test_dry_run_changes_nothing(self):
        prim, rep = populated_pair()
        before = state(rep)
        self.assertTrue(ps.run_once(config(endpoint(prim), endpoint(rep), dry_run=True)))
        self.assertEqual(state(rep), before)
        self.assertEqual(rep.writes(), [])

    def test_empty_primary_does_not_wipe_replicas(self):
        prim, rep = populated_pair()
        prim.lists.clear()
        self.assertFalse(ps.run_once(config(endpoint(prim), endpoint(rep))))
        self.assertTrue(rep.lists)

    def test_replica_down_does_not_stop_others(self):
        prim, rep = populated_pair()
        cfg = config(endpoint(prim), "http://127.0.0.1:9|x", endpoint(rep))
        self.assertFalse(ps.run_once(cfg))
        self.assertEqual(state(rep), state(prim))

    def test_without_password(self):
        prim, rep = start(FTL()), start(FTL(id_offset=500))
        prim.lists.append({"id": 1, "address": "https://x/l", "type": "block", "comment": None,
                           "groups": [0], "enabled": True})
        self.assertTrue(ps.run_once(config(prim.url, rep.url)))
        self.assertEqual(len(rep.lists), 1)
        self.assertFalse(any(c[1] == "auth" for c in prim.calls + rep.calls))

    def test_config_keys(self):
        self.assertIn("misc.dnsmasq_lines", ps.parse_config_keys("+misc.dnsmasq_lines"))
        self.assertNotIn("dns.rateLimit", ps.parse_config_keys("-dns.rateLimit"))
        self.assertEqual(ps.parse_config_keys("dns.hosts, dns.upstreams"), ["dns.hosts", "dns.upstreams"])


if __name__ == "__main__":
    logging.basicConfig(level="CRITICAL")
    unittest.main()
