# pihole-sync

**English** · [Dansk](README.da.md)

Keeps one or more Pi-hole v6 replicas in sync with a primary Pi-hole. It's a replacement
for [nebula-sync](https://github.com/lovelaze/nebula-sync) that only changes what actually differs.

It's a single Python file with no dependencies (Python 3.8+), and it can run from Docker, systemd or cron.

## Why not nebula-sync?

nebula-sync imports a Teleporter backup on every replica on every run. That restarts DNS,
often runs gravity, and leaves sessions behind. This causes most of the problems in its
issue tracker: "invalid sid", "API seats exceeded", 502s after restarts and broken gravity databases.

pihole-sync instead reads the primary and each replica through the API, works out the
differences and fixes only those:

| | nebula-sync | pihole-sync |
|---|---|---|
| Nothing changed | Full import, DNS restarted | Only GET requests, no interruption |
| Gravity | On every sync, also on the primary | Only on a replica whose adlists changed |
| Groups | Database IDs copied | Matched by name, IDs translated |
| Sessions | Often left open | Always logged out, renewed after an FTL restart |
| Replica down | Can stop the whole run | Error logged, the others are synced |
| Primary empty/reset | Wipes the replicas | Stops (unless `ALLOW_EMPTY=1`) |
| Dry-run | No | `--dry-run` / `DRY_RUN=1` |

**What is synced:** groups, adlists, allow/deny domains (exact and regex), clients and a set of
settings: upstreams, local DNS records, CNAMEs, conditional forwarding, DNSSEC and more.
Host-specific settings such as interface, webserver, DHCP and passwords are not synced.
Anything that only exists on a replica is removed, so each replica mirrors the primary.

## Setup

```bash
git clone https://github.com/Tntdruid/pihole-sync.git
cd pihole-sync
cp sync.env.example sync.env   # and fill it in
./pihole-sync.py --dry-run
```

`sync.env` next to the script is read automatically. With `--dry-run`, the script shows
what it would change without changing anything. When the output looks right, run it in one
of these ways:

**Docker**

```bash
docker compose up -d --build
docker logs -f pihole-sync
```

**systemd:** syncs every `INTERVAL` seconds. See [pihole-sync.service](pihole-sync.service).

**cron**

```
*/5 * * * * root /usr/bin/python3 /opt/pihole-sync/pihole-sync.py --once
```

### Settings

All settings are environment variables, or lines in `sync.env`:

| Variable | Default | |
|---|---|---|
| `PRIMARY` | | `https://192.168.1.2\|password`. `\|file:/path` reads the password from a file. Leave out `\|password` if there is none. |
| `REPLICAS` | | Same format, comma-separated |
| `INTERVAL` | `0` | Seconds between runs. `0` = run once |
| `SYNC` | `lists,domains,clients,config` | Also available: `blocking` (follow the primary's blocking on/off state) |
| `CONFIG_KEYS` | see the script | `+key` adds, `-key` removes, e.g. `+misc.dnsmasq_lines` |
| `VERIFY_TLS` | `1` | `0` for self-signed certificates |
| `DRY_RUN` | `0` | |
| `ALLOW_EMPTY` | `0` | Allow a primary without adlists to empty the replicas |
| `HEALTHCHECK_URL` | | Pinged after every run, with `/fail` appended on errors |
| `LOG_LEVEL` | `INFO` | |

**Pi-hole without a password:** Pi-hole then only answers to its own names (IP address,
`pi.hole`, `webserver.domain` or hostname). If you get `HTTP 401 ... No password is set`,
either use the IP address or set `webserver.domain` on that Pi-hole.

### Moving from nebula-sync

`PRIMARY` and `REPLICAS` use the same format, so they can be copied over directly. Stop
nebula-sync, run pihole-sync with `--dry-run` first, then start it for real.

## kea-to-pihole: hostnames from OPNsense

[kea-to-pihole/](kea-to-pihole/) contains a small bash script. It reads hostnames from Kea DHCP
on OPNsense (reservations and leases, IPv4 and IPv6) and writes them as local DNS records
(`dns.hosts`) on the primary Pi-hole. pihole-sync then passes them on to the replicas.

Manual records are left alone. If you have a manual record for the same hostname and IP
version, it wins over the Kea record.

```bash
cp kea-to-pihole/kea-to-pihole.sh /usr/local/bin/
cp kea-to-pihole/kea-to-pihole.conf.example /etc/kea-to-pihole.conf   # and fill it in
kea-to-pihole.sh
```

It requires `curl` and `jq`. Run it from cron, e.g. every 5 minutes.

## Tests

```bash
python3 tests/test_pihole_sync.py
python3 tests/test_kea_to_pihole.py   # requires bash, curl and jq
```

The tests run against a fake Pi-hole API, so they need no real Pi-hole.

## License

MIT
