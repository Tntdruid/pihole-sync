# pihole-sync

[English](README.md) · **Dansk**

Holder en eller flere Pi-hole v6-replicas synkroniseret med en primær Pi-hole. Det er en
erstatning for [nebula-sync](https://github.com/lovelaze/nebula-sync), der kun ændrer det,
der faktisk er forskelligt.

Det er én Python-fil uden afhængigheder (Python 3.8+), og den kan køre fra Docker, systemd eller cron.

## Hvorfor ikke nebula-sync?

nebula-sync importerer en Teleporter-backup på hver replica ved hver kørsel. Det genstarter
DNS, kører ofte gravity og efterlader sessioner. Det er årsagen til de fleste problemer i dens
issue-tracker: "invalid sid", "API seats exceeded", 502 efter genstart og ødelagte gravity-databaser.

pihole-sync læser i stedet primæren og hver replica via API'et, finder forskellene og
retter kun dem:

| | nebula-sync | pihole-sync |
|---|---|---|
| Intet ændret | Fuld import, DNS genstartes | Kun GET-kald, ingen afbrydelse |
| Gravity | Ved hver sync, også på primæren | Kun på en replica, hvis dens adlister er ændret |
| Grupper | Database-ID'er kopieres | Matches på navn, ID'er oversættes |
| Sessioner | Bliver ofte hængende | Logges altid ud, fornyes efter FTL-genstart |
| Replica nede | Kan stoppe hele kørslen | Fejlen logges, de andre synkroniseres |
| Primær tom/nulstillet | Tømmer replicas | Stopper (medmindre `ALLOW_EMPTY=1`) |
| Dry-run | Nej | `--dry-run` / `DRY_RUN=1` |

**Det synkroniseres:** grupper, adlister, allow/deny-domæner (exact og regex), klienter og et
udvalg af indstillinger: upstreams, lokale DNS-records, CNAME'er, conditional forwarding, DNSSEC m.m.
Værtsspecifikke indstillinger som interface, webserver, DHCP og adgangskoder synkroniseres ikke.
Alt, der kun findes på en replica, bliver fjernet, så hver replica spejler primæren.

## Opsætning

```bash
git clone https://github.com/Tntdruid/pihole-sync.git
cd pihole-sync
cp sync.env.example sync.env   # og udfyld den
./pihole-sync.py --dry-run
```

`sync.env` ved siden af scriptet bliver læst automatisk. Med `--dry-run` viser scriptet, hvad
det ville ændre, uden at ændre noget. Når outputtet ser rigtigt ud, kan du køre det på en af
disse måder:

**Docker**

```bash
docker compose up -d --build
docker logs -f pihole-sync
```

**systemd:** synkroniserer hvert `INTERVAL` sekund. Se [pihole-sync.service](pihole-sync.service).

**cron**

```
*/5 * * * * root /usr/bin/python3 /opt/pihole-sync/pihole-sync.py --once
```

### Indstillinger

Alle indstillinger er miljøvariabler eller linjer i `sync.env`:

| Variabel | Standard | |
|---|---|---|
| `PRIMARY` | | `https://192.168.1.2\|adgangskode`. `\|file:/sti` læser adgangskoden fra en fil. Udelad `\|adgangskode`, hvis der ingen er. |
| `REPLICAS` | | Samme format, kommasepareret |
| `INTERVAL` | `0` | Sekunder mellem kørsler. `0` = kør én gang |
| `SYNC` | `lists,domains,clients,config` | Kan også tage `blocking` (følg primærens blocking til/fra) |
| `CONFIG_KEYS` | se scriptet | `+nøgle` tilføjer, `-nøgle` fjerner, fx `+misc.dnsmasq_lines` |
| `VERIFY_TLS` | `1` | `0` ved selvsignerede certifikater |
| `DRY_RUN` | `0` | |
| `ALLOW_EMPTY` | `0` | Tillad, at en primær uden adlister tømmer replicas |
| `HEALTHCHECK_URL` | | Pinges efter hver kørsel, med `/fail` tilføjet ved fejl |
| `LOG_LEVEL` | `INFO` | |

**Pi-hole uden adgangskode:** Pi-hole svarer så kun på sine egne navne (IP-adresse,
`pi.hole`, `webserver.domain` eller hostname). Får du `HTTP 401 ... No password is set`, så
brug IP-adressen, eller sæt `webserver.domain` på den Pi-hole.

### Skift fra nebula-sync

`PRIMARY` og `REPLICAS` har samme format, så de kan kopieres direkte. Stop nebula-sync, kør
først pihole-sync med `--dry-run`, og start den så rigtigt.

## kea-to-pihole: hostnames fra OPNsense

[kea-to-pihole/](kea-to-pihole/) indeholder et lille bash-script. Det henter hostnames fra
Kea DHCP på OPNsense (reservationer og leases, IPv4 og IPv6) og skriver dem som lokale
DNS-records (`dns.hosts`) på den primære Pi-hole. pihole-sync sender dem derefter videre til replicas.

Manuelle records bliver ikke rørt. Har du en manuel record for samme hostname og IP-type,
vinder den over Kea-recorden.

```bash
cp kea-to-pihole/kea-to-pihole.sh /usr/local/bin/
cp kea-to-pihole/kea-to-pihole.conf.example /etc/kea-to-pihole.conf   # og udfyld den
kea-to-pihole.sh
```

Det kræver `curl` og `jq`. Kør det fra cron, fx hvert 5. minut.

## Tests

```bash
python3 tests/test_pihole_sync.py
python3 tests/test_kea_to_pihole.py   # kræver bash, curl og jq
```

Testene kører mod et falsk Pi-hole-API, så de kræver ingen rigtig Pi-hole.

## Licens

MIT
