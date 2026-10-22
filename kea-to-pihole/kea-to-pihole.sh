#!/usr/bin/env bash
# Syncs hostnames from Kea DHCP on OPNsense to Pi-hole's local DNS records (dns.hosts) via the API.
# Point it at the primary Pi-hole - pihole-sync passes the records on to the replicas.
# Requires: curl, jq. Runs on any machine that can reach both OPNsense and Pi-hole.
#
# Manual records in Pi-hole (Settings -> Local DNS Records) are left alone. The script remembers
# in STATE_FILE which lines it wrote itself, and a manual record for the same hostname wins.
#
# Settings are read from kea-to-pihole.conf next to the script, or from the file named in
# $KEA_TO_PIHOLE_CONFIG, or from /etc/kea-to-pihole.conf. See kea-to-pihole.conf.example.
#
# Cron (e.g. every 5 minutes):
#   */5 * * * * root /usr/local/bin/kea-to-pihole.sh
set -euo pipefail

# --- Defaults (override in the config file) --------------------------------
OPN_URL=""                             # OPNsense, e.g. https://192.168.1.1
OPN_KEY=""
OPN_SECRET=""
OPN_INSECURE=1                         # 1 = accept a self-signed certificate on OPNsense
DOMAIN=""                              # e.g. home.example.com
INCLUDE_LEASES=1                       # 1 = also include dynamic leases, 0 = reservations only
INCLUDE_V6=1                           # 1 = also fetch from Kea DHCPv6

PIHOLE_URL=""                          # the primary Pi-hole, e.g. https://192.168.1.2
PIHOLE_PASSWORD=""                     # app password, empty if Pi-hole has no password
PIHOLE_INSECURE=1                      # 1 = accept a self-signed certificate on Pi-hole

STATE_FILE="/var/lib/kea-to-pihole/managed.hosts"   # the lines the script wrote last time
# ---------------------------------------------------------------------------

for conf in "${KEA_TO_PIHOLE_CONFIG:-}" "$(dirname "$(readlink -f "$0")")/kea-to-pihole.conf" /etc/kea-to-pihole.conf; do
    if [[ -n $conf && -f $conf ]]; then
        # shellcheck source=/dev/null
        source "$conf"
        break
    fi
done

for var in OPN_URL OPN_KEY OPN_SECRET DOMAIN PIHOLE_URL; do
    if [[ -z ${!var} ]]; then
        echo "$var is not set - see kea-to-pihole.conf.example" >&2
        exit 2
    fi
done

opn_opts=(-fsS --max-time 20 -u "$OPN_KEY:$OPN_SECRET" -H 'Content-Type: application/json')
[[ $OPN_INSECURE == 1 ]] && opn_opts+=(-k)
ph_opts=(-fsS --max-time 30 -H 'Content-Type: application/json')
[[ $PIHOLE_INSECURE == 1 ]] && ph_opts+=(-k)

tmp=$(mktemp -d)
sid=""
cleanup() {
    # Always log out again, so Pi-hole doesn't run out of API sessions
    if [[ -n $sid ]]; then
        curl "${ph_opts[@]}" -X DELETE "$PIHOLE_URL/api/auth" > /dev/null 2>&1 || true
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT

api() {
    curl "${opn_opts[@]}" -d '{"current":1,"rowCount":-1,"searchPhrase":""}' "$OPN_URL/api/$1"
}

# Fetches "IP hostname" lines: reservations first, so they win over leases for the same hostname
fetch() {  # $1 = 4 or 6
    api "kea/dhcpv$1/search_reservation" \
        | jq -r '.rows[] | select((.hostname // "") != "" and (.ip_address // "") != "") | "\(.ip_address) \(.hostname)"'
    if [[ $INCLUDE_LEASES == 1 ]]; then
        api "kea/leases$1/search" \
            | jq -r '.rows[] | select((.hostname // "") != "" and (.address // "") != "") | "\(.address) \(.hostname)"'
    fi
}

# Normalise: first label only, lowercase, valid characters, one IP per hostname
normalise() {
    awk -v dom="$DOMAIN" '
    {
        ip = $1; h = tolower($2)
        sub(/\..*/, "", h)
        gsub(/[^a-z0-9-]/, "", h)
        if (h == "" || seen[h]++) next
        print ip, h "." dom, h
    }' "$1"
}

fetch 4 > "$tmp/raw4"
normalise "$tmp/raw4" | sort -t . -k1,1n -k2,2n -k3,3n -k4,4n > "$tmp/kea"

if [[ $INCLUDE_V6 == 1 ]]; then
    # If the DHCPv6 call fails, we still keep the IPv4 list
    fetch 6 > "$tmp/raw6" || { echo "Could not fetch from Kea DHCPv6 - skipping IPv6" >&2; : > "$tmp/raw6"; }
    normalise "$tmp/raw6" | sort -k2,2 >> "$tmp/kea"
fi

# Don't replace a good list with an empty one if the API answers strangely
if [[ ! -s $tmp/kea ]]; then
    echo "No hosts found in Kea - leaving Pi-hole as it is" >&2
    exit 1
fi

# --- Pi-hole ---------------------------------------------------------------
# Empty PIHOLE_PASSWORD = Pi-hole without a password, so no login
if [[ -n $PIHOLE_PASSWORD ]]; then
    sid=$(jq -n --arg pw "$PIHOLE_PASSWORD" '{password: $pw}' \
        | curl "${ph_opts[@]}" -d @- "$PIHOLE_URL/api/auth" | jq -r '.session.sid // empty')
    if [[ -z $sid ]]; then
        echo "Login to Pi-hole failed" >&2
        exit 1
    fi
    ph_opts+=(-H "X-FTL-SID: $sid")
fi

curl "${ph_opts[@]}" "$PIHOLE_URL/api/config" | jq -r '.config.dns.hosts[]' > "$tmp/current"

prev="$STATE_FILE"
[[ -f $prev ]] || prev=/dev/null

# New dns.hosts: the manual lines (everything we didn't write ourselves) first, then the Kea lines.
# A Kea line is skipped if a manual line already uses the same hostname with the same IP version -
# a manual IPv6 record for "nas" should not remove Kea's IPv4 address, and vice versa.
awk -v keaout="$tmp/kea.written" '
    function fam(ip) { return index(ip, ":") ? 6 : 4 }
    FILENAME == ARGV[1] { ours[$0] = 1; next }
    FILENAME == ARGV[2] { kea[++n] = $0; fromkea[$0] = 1; next }
    {
        if ($0 in ours || $0 in fromkea) next
        for (i = 2; i <= NF; i++) manual[fam($1), tolower($i)] = 1
        print
    }
    END {
        for (i = 1; i <= n; i++) {
            split(kea[i], f, " ")
            if (((fam(f[1]), f[2]) in manual) || ((fam(f[1]), f[3]) in manual)) continue
            print kea[i]
            print kea[i] > keaout
        }
    }' "$prev" "$tmp/kea" "$tmp/current" > "$tmp/desired"

touch "$tmp/kea.written"
mkdir -p "$(dirname "$STATE_FILE")"
cmp -s "$tmp/kea.written" "$STATE_FILE" 2> /dev/null || install -m 644 "$tmp/kea.written" "$STATE_FILE"

if cmp -s "$tmp/current" "$tmp/desired"; then
    exit 0
fi

jq -R . "$tmp/desired" | jq -s '{config: {dns: {hosts: .}}}' \
    | curl "${ph_opts[@]}" -X PATCH -d @- "$PIHOLE_URL/api/config" > /dev/null
echo "Updated dns.hosts on $PIHOLE_URL: $(wc -l < "$tmp/kea.written") from Kea, $(( $(wc -l < "$tmp/desired") - $(wc -l < "$tmp/kea.written") )) manual"
