#!/usr/bin/env python3
"""
pihole-sync - keeps one or more Pi-hole v6 replicas in sync with a primary Pi-hole.

How it differs from nebula-sync:
  * No Teleporter. Each replica is compared with the primary through the API and only the
    differences are written. If nothing changed, only GET requests are made: no DNS restart,
    no gravity run.
  * Gravity only runs on a replica when its adlists actually changed - never on the primary.
  * Groups are matched by name (not database ID), so assignments come out right even though
    the IDs differ between Pi-holes.
  * Sessions are always logged out, also on errors, and the primary is only held open while
    it is being read.
  * A replica that is down does not stop the others. 502s and refused connections are retried,
    and a session that expired (e.g. after an FTL restart) is renewed automatically.
  * Nothing is ever written to the primary, and an empty primary cannot wipe the replicas.
  * Dry-run mode, and passwords are never logged.

Requires only Python 3.8+ (standard library). See README.md for setup.
"""
import argparse
import json
import logging
import os
import re
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

log = logging.getLogger("pihole-sync")

ALL_SYNC = {"lists", "domains", "clients", "config", "blocking"}
DEFAULT_SYNC = "lists,domains,clients,config"

# Settings that make sense to keep identical on all Pi-holes. Host-specific settings
# (interface, webserver, DHCP, NTP, passwords, misc.dnsmasq_lines) are deliberately left out.
DEFAULT_CONFIG_KEYS = [
    "dns.upstreams",
    "dns.hosts",
    "dns.cnameRecords",
    "dns.revServers",
    "dns.domain",
    "dns.domainNeeded",
    "dns.expandHosts",
    "dns.bogusPriv",
    "dns.dnssec",
    "dns.blockESNI",
    "dns.blocking.mode",
    "dns.specialDomains",
    "dns.rateLimit",
]

# Group 0 ("Default") always exists and may have been renamed, so it is matched by ID instead of name
DEFAULT_GROUP = "\0default"
_MISSING = object()


class ApiError(Exception):
    pass


def q(value):
    return urllib.parse.quote(str(value), safe="")


def api_error_text(err):
    try:
        e = json.loads(err.read())["error"]
        return " - ".join(str(x) for x in (e.get("key"), e.get("message"), e.get("hint")) if x)
    except Exception:
        return err.reason or ""


class PiHole:
    def __init__(self, url, password, verify_tls=True, timeout=30):
        self.url = url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.sid = None
        self.ctx = None if verify_tls else ssl._create_unverified_context()

    def __str__(self):
        return urllib.parse.urlsplit(self.url).netloc or self.url

    def request(self, method, path, body=None, query=None, raw=False, timeout=None, attempts=6):
        url = f"{self.url}/api/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        relogged = False
        for attempt in range(1, attempts + 1):
            headers = {"Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            if self.sid:
                headers["X-FTL-SID"] = self.sid
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self.ctx) as resp:
                    payload = resp.read()
            except urllib.error.HTTPError as e:
                detail = api_error_text(e)
                # The session expired, e.g. after an FTL restart: log in again, once
                if e.code == 401 and self.sid and path != "auth" and not relogged:
                    relogged = True
                    log.debug("%s: session expired, logging in again", self)
                    self.login()
                    continue
                # FTL is (re)starting
                if e.code in (502, 503, 504) and attempt < attempts:
                    time.sleep(min(2 * attempt, 10))
                    continue
                raise ApiError(f"{method} /api/{path}: HTTP {e.code} {detail}".rstrip()) from None
            except (urllib.error.URLError, OSError) as e:
                reason = getattr(e, "reason", e)
                # A refused connection never reached the server, so it is safe to retry.
                # Timeouts and the like are only retried for GET, so a POST can never run twice.
                if (method == "GET" or isinstance(reason, ConnectionRefusedError)) and attempt < attempts:
                    time.sleep(min(2 * attempt, 10))
                    continue
                raise ApiError(f"{method} /api/{path}: {reason}") from None

            if raw:
                return payload.decode(errors="replace")
            res = json.loads(payload) if payload else None
            # FTL may answer 2xx but report per-item errors in "processed.errors"
            errors = res.get("processed", {}).get("errors") if isinstance(res, dict) else None
            if errors:
                raise ApiError(f"{method} /api/{path}: {errors}")
            return res
        raise ApiError(f"{method} /api/{path}: gave up after {attempts} attempts")

    def get(self, path, key):
        return self.request("GET", path)[key]

    def login(self):
        self.sid = None
        if not self.password:
            return  # Pi-hole without a password
        try:
            res = self.request("POST", "auth", {"password": self.password})
        except ApiError as e:
            if "HTTP 429" in str(e):
                raise ApiError(f"{e} (all API sessions are in use - see webserver.api.max_sessions)") from None
            raise
        session = (res or {}).get("session", {})
        if not session.get("valid"):
            raise ApiError("login rejected - wrong password?")
        self.sid = session.get("sid")

    def logout(self):
        if not self.sid:
            return
        try:
            self.request("DELETE", "auth", attempts=3)
        except ApiError as e:
            # 401 = the session is already gone, typically because FTL restarted
            (log.debug if "HTTP 401" in str(e) else log.warning)("%s: logout failed: %s", self, e)
        finally:
            self.sid = None

    def __enter__(self):
        self.login()
        return self

    def __exit__(self, *exc):
        self.logout()


# --- Reading state --------------------------------------------------------------

def group_keys(groups):
    """Group ID -> key (the name, except for group 0)."""
    return {g["id"]: DEFAULT_GROUP if g["id"] == 0 else g["name"] for g in groups}


def read_state(ph, sync):
    """Fetches everything that is synced, with group IDs translated to names."""
    groups = ph.get("groups", "groups")
    keys = group_keys(groups)

    def gset(ids):
        return tuple(sorted(keys.get(i, f"#{i}") for i in ids))

    state = {"groups": {
        keys[g["id"]]: {"name": g["name"], "comment": g.get("comment") or None, "enabled": bool(g["enabled"])}
        for g in groups
    }}
    if "lists" in sync:
        state["lists"] = {
            (x["address"], x["type"]): {"comment": x.get("comment") or None, "enabled": bool(x["enabled"]),
                                        "groups": gset(x["groups"])}
            for x in ph.get("lists", "lists")
        }
    if "domains" in sync:
        state["domains"] = {
            (x["type"], x["kind"], x["domain"]): {"comment": x.get("comment") or None, "enabled": bool(x["enabled"]),
                                                  "groups": gset(x["groups"])}
            for x in ph.get("domains", "domains")
        }
    if "clients" in sync:
        state["clients"] = {
            x["client"]: {"comment": x.get("comment") or None, "groups": gset(x["groups"])}
            for x in ph.get("clients", "clients")
        }
    if "config" in sync:
        state["config"] = ph.get("config", "config")
    if "blocking" in sync:
        state["blocking"] = ph.request("GET", "dns/blocking")
    return state


def get_path(d, path):
    for part in path.split("."):
        if not isinstance(d, dict) or part not in d:
            return _MISSING
        d = d[part]
    return d


def set_path(d, path, value):
    *parents, last = path.split(".")
    for part in parents:
        d = d.setdefault(part, {})
    d[last] = value


# --- Syncing one replica --------------------------------------------------------

def sync_replica(rep, P, cfg):
    """Makes the replica match the primary. Returns the number of changes."""
    changes = 0

    def do(desc, method, path, body=None, query=None, **kw):
        nonlocal changes
        changes += 1
        log.info("%s: %s%s", rep, "[dry-run] " if cfg.dry_run else "", desc)
        if not cfg.dry_run:
            return rep.request(method, path, body, query, **kw)

    with rep:
        R = read_state(rep, cfg.sync)

        # 1. Groups are created/updated first, so they can be assigned below
        for key, g in P["groups"].items():
            cur = R["groups"].get(key)
            if cur == g:
                continue
            body = {"name": g["name"], "comment": g["comment"], "enabled": g["enabled"]}
            if cur is None:
                do(f"create group '{g['name']}'", "POST", "groups", body)
            else:
                do(f"update group '{g['name']}'", "PUT", f"groups/{q(cur['name'])}", body)

        # The replica's own group IDs (in dry-run nothing is sent, so the IDs don't matter)
        if cfg.dry_run:
            gid = {k: n for n, k in enumerate(P["groups"])}
        else:
            gid = {k: i for i, k in group_keys(rep.get("groups", "groups")).items()}

        def ids(names):
            return [gid[n] for n in names if n in gid]

        def mirror(kind, describe, create, update, delete):
            want, have = P[kind], R[kind]
            for key, item in want.items():
                if key not in have:
                    do(f"add {describe(key)}", *create(key, item))
                elif have[key] != item:
                    do(f"update {describe(key)}", *update(key, item))
            for key in have.keys() - want.keys():
                do(f"remove {describe(key)}", *delete(key))

        # 2. Adlists, domains and clients
        gravity = False
        if "lists" in cfg.sync:
            # New, removed or (de)activated adlists need gravity; comments/groups don't
            gravity = P["lists"].keys() != R["lists"].keys() or any(
                R["lists"][k]["enabled"] != v["enabled"] for k, v in P["lists"].items() if k in R["lists"])
            mirror(
                "lists",
                lambda k: f"{k[1]}list {k[0]}",
                lambda k, v: ("POST", "lists", {"address": k[0], "comment": v["comment"],
                                                "groups": ids(v["groups"]), "enabled": v["enabled"]}, {"type": k[1]}),
                lambda k, v: ("PUT", f"lists/{q(k[0])}", {"comment": v["comment"], "type": k[1],
                                                          "groups": ids(v["groups"]), "enabled": v["enabled"]},
                              {"type": k[1]}),
                lambda k: ("DELETE", f"lists/{q(k[0])}", None, {"type": k[1]}),
            )
        if "domains" in cfg.sync:
            mirror(
                "domains",
                lambda k: f"{k[0]}/{k[1]} {k[2]}",
                lambda k, v: ("POST", f"domains/{k[0]}/{k[1]}", {"domain": k[2], "comment": v["comment"],
                                                                 "groups": ids(v["groups"]), "enabled": v["enabled"]}),
                lambda k, v: ("PUT", f"domains/{k[0]}/{k[1]}/{q(k[2])}", {
                    "type": k[0], "kind": k[1], "comment": v["comment"],
                    "groups": ids(v["groups"]), "enabled": v["enabled"]}),
                lambda k: ("DELETE", f"domains/{k[0]}/{k[1]}/{q(k[2])}"),
            )
        if "clients" in cfg.sync:
            mirror(
                "clients",
                lambda k: f"client {k}",
                lambda k, v: ("POST", "clients", {"client": k, "comment": v["comment"], "groups": ids(v["groups"])}),
                lambda k, v: ("PUT", f"clients/{q(k)}", {"comment": v["comment"], "groups": ids(v["groups"])}),
                lambda k: ("DELETE", f"clients/{q(k)}"),
            )

        # 3. Groups that don't exist on the primary are removed last, when nothing uses them anymore
        for key in R["groups"].keys() - P["groups"].keys():
            if key != DEFAULT_GROUP:
                name = R["groups"][key]["name"]
                do(f"remove group '{name}'", "DELETE", f"groups/{q(name)}")

        # 4. Gravity, but only if the adlists changed
        if gravity:
            out = do("run gravity (adlists changed)", "POST", "action/gravity", raw=True, timeout=900, attempts=1)
            if out and "[✗]" in out:
                log.warning("%s: gravity reported errors:\n%s", rep,
                            "\n".join(line for line in out.splitlines() if "[✗]" in line))

        # 5. Blocking state (e.g. when blocking is disabled for 5 minutes on the primary)
        if "blocking" in cfg.sync:
            pb, rb = P["blocking"], R["blocking"]
            if pb.get("blocking") in ("enabled", "disabled") and pb.get("blocking") != rb.get("blocking"):
                do(f"blocking {pb['blocking']}", "POST", "dns/blocking",
                   {"blocking": pb["blocking"] == "enabled", "timer": pb.get("timer")})

        # 6. Settings last - a PATCH may restart FTL on the replica
        if "config" in cfg.sync:
            patch, changed = {}, []
            for key in cfg.config_keys:
                pv, rv = get_path(P["config"], key), get_path(R["config"], key)
                if pv is _MISSING or rv is _MISSING:
                    log.debug("%s: setting %s does not exist on both - skipping", rep, key)
                elif pv != rv:
                    set_path(patch, key, pv)
                    changed.append(key)
            if patch:
                do(f"update settings: {', '.join(changed)}", "PATCH", "config", {"config": patch})

    return changes


def run_once(cfg):
    start = time.monotonic()
    try:
        with cfg.primary:
            P = read_state(cfg.primary, cfg.sync)
    except Exception as e:
        log.error("Cannot read the primary %s: %s - not touching the replicas", cfg.primary, e)
        return False

    # A reset or half-started primary must not be able to wipe all replicas
    if "lists" in cfg.sync and not P["lists"] and not cfg.allow_empty:
        log.error("The primary %s has no adlists - stopping (set ALLOW_EMPTY=1 if this is intended)", cfg.primary)
        return False

    ok, total = True, 0
    for rep in cfg.replicas:
        try:
            total += sync_replica(rep, P, cfg)
        except Exception as e:
            log.error("%s: sync failed: %s", rep, e)
            log.debug("Details", exc_info=True)
            ok = False
    log.info("Sync %s in %.1f s - %d replica(s), %d change(s)%s",
             "finished" if ok else "partially failed", time.monotonic() - start, len(cfg.replicas), total,
             " (dry-run)" if cfg.dry_run else "")
    return ok


# --- Configuration --------------------------------------------------------------

def env_bool(name, default=False):
    value = os.environ.get(name)
    return default if value is None or value == "" else value.strip().lower() in ("1", "true", "yes", "on")


def load_env_file(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)  # real environment variables win


def parse_endpoint(spec, verify_tls):
    """'https://pihole.lan|password' or 'https://pihole.lan|file:/run/secrets/pw'"""
    url, _, password = spec.strip().partition("|")
    if not re.match(r"https?://", url):
        raise ValueError(f"invalid URL: {url!r} (must start with http:// or https://)")
    if password.startswith("file:"):
        with open(password[5:], encoding="utf-8") as f:
            password = f.read().strip()
    return PiHole(url, password, verify_tls)


def parse_config_keys(raw):
    items = [x for x in re.split(r"[,\s]+", raw) if x]
    if not items:
        return list(DEFAULT_CONFIG_KEYS)
    if not all(x[0] in "+-" for x in items):
        return items  # a plain list replaces the default list entirely
    keys = list(DEFAULT_CONFIG_KEYS)
    for x in items:
        name = x[1:]
        if x[0] == "+" and name not in keys:
            keys.append(name)
        elif x[0] == "-":
            keys = [k for k in keys if k != name and not k.startswith(name + ".")]
    return keys


def load_config(args):
    env = os.environ
    verify = env_bool("VERIFY_TLS", True)
    if not env.get("PRIMARY"):
        raise ValueError("PRIMARY is not set")
    primary = parse_endpoint(env["PRIMARY"], verify)
    # Comma-separated like nebula-sync, but only split in front of a URL, so commas in passwords work
    replicas = [parse_endpoint(r, verify) for r in re.split(r"[\s,]+(?=https?://)", env.get("REPLICAS", "").strip()) if r]
    if not replicas:
        raise ValueError("REPLICAS is not set")
    if any(r.url == primary.url for r in replicas):
        raise ValueError("the primary is also listed as a replica")

    sync = {s.strip().lower() for s in env.get("SYNC", DEFAULT_SYNC).split(",") if s.strip()} - {"groups"}
    unknown = sync - ALL_SYNC
    if unknown:
        raise ValueError(f"unknown in SYNC: {', '.join(sorted(unknown))} (valid: {', '.join(sorted(ALL_SYNC))})")

    return SimpleNamespace(
        primary=primary,
        replicas=replicas,
        sync=sync,
        config_keys=parse_config_keys(env.get("CONFIG_KEYS", "")),
        dry_run=args.dry_run or env_bool("DRY_RUN"),
        allow_empty=env_bool("ALLOW_EMPTY"),
        interval=0 if args.once else int(env.get("INTERVAL") or 0),
        healthcheck=env.get("HEALTHCHECK_URL", "").strip(),
    )


def ping_healthcheck(url, ok):
    if not url:
        return
    try:
        urllib.request.urlopen(url if ok else url.rstrip("/") + "/fail", timeout=10).close()
    except OSError as e:
        log.warning("Healthcheck ping failed: %s", e)


def main():
    ap = argparse.ArgumentParser(description="Keeps Pi-hole v6 replicas in sync with a primary Pi-hole.")
    ap.add_argument("--env-file", help="file with KEY=VALUE lines (default: sync.env next to the script)")
    ap.add_argument("--once", action="store_true", help="run once, even if INTERVAL is set")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, without changing anything")
    args = ap.parse_args()

    default_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.env")
    if not args.env_file and os.path.isfile(default_env):
        args.env_file = default_env

    try:
        if args.env_file:
            load_env_file(args.env_file)
        logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                            format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        cfg = load_config(args)
    except (ValueError, OSError) as e:
        logging.basicConfig()
        log.error("Configuration: %s", e)
        return 2

    # docker stop sends SIGTERM; as SystemExit the with-blocks still get to log out
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("Primary: %s, replicas: %s, syncing: %s", cfg.primary,
             ", ".join(map(str, cfg.replicas)), ", ".join(sorted(cfg.sync)))
    while True:
        ok = run_once(cfg)
        ping_healthcheck(cfg.healthcheck, ok)
        if not cfg.interval:
            return 0 if ok else 1
        time.sleep(cfg.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
