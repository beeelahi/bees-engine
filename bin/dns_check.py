"""
DNS health of the website's domain (Python 3.9 stdlib only, raw UDP queries, read-only).

    python3 ~/BinanceData/bin/dns_check.py [domain]          (ops: dns-check)

1. asks the .xyz registry servers who the domain's nameservers are (the delegation);
2. asks each of those nameservers directly, several times, for the domain's A records;
3. asks the big public resolvers (Google, Cloudflare, Quad9, OpenDNS) several times.
An intermittent "this site can't be reached / NXDOMAIN" usually means one delegated nameserver does not know the
domain (an old or wrong nameserver still listed at the registrar), so resolvers that happen to ask it get NXDOMAIN.
"""
import random
import socket
import struct
import sys

RCODE = {0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
PUBLIC = {"Google 8.8.8.8": "8.8.8.8", "Google 8.8.4.4": "8.8.4.4", "Cloudflare 1.1.1.1": "1.1.1.1", "Cloudflare 1.0.0.1": "1.0.0.1",
          "Quad9 9.9.9.9": "9.9.9.9", "OpenDNS 208.67.222.222": "208.67.222.222"}


def query(server, name, qtype=1, timeout=3, rd=True):
    tid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", tid, 0x0100 if rd else 0, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.strip(".").split(".")) + b"\0" + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    return parse(data)


def query_raw(server, name, qtype):
    """The SOA record's last field (minimum = how long resolvers cache a 'does not exist')."""
    tid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", tid, 0, 1, 0, 0, 0) + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(q, (server, 53))
        d, _ = s.recvfrom(4096)
    finally:
        s.close()
    i = 12
    _, i = _name(d, i)
    i += 4
    _, i = _name(d, i)
    typ, _, ttl, ln = struct.unpack(">HHIH", d[i:i + 10])
    return struct.unpack(">I", d[i + 10 + ln - 4:i + 10 + ln])[0]


def _name(d, i):
    out, jumped, end = [], False, None
    while True:
        n = d[i]
        if n == 0:
            i += 1
            break
        if n & 0xC0 == 0xC0:
            if not jumped:
                end = i + 2
            i, jumped = ((n & 0x3F) << 8) | d[i + 1], True
            continue
        out.append(d[i + 1:i + 1 + n].decode(errors="replace"))
        i += 1 + n
    return ".".join(out), (end if jumped else i)


def parse(d):
    _, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", d[:12])
    i = 12
    for _ in range(qd):
        _, i = _name(d, i)
        i += 4
    recs = []
    for sec, cnt in (("an", an), ("ns", ns), ("ar", ar)):
        for _ in range(cnt):
            nm, i = _name(d, i)
            typ, _, ttl, ln = struct.unpack(">HHIH", d[i:i + 10])
            i += 10
            rd = d[i:i + ln]
            val = None
            if typ == 1 and ln == 4:
                val = socket.inet_ntoa(rd)
            elif typ in (2, 5):
                val, _ = _name(d, i)
            recs.append((sec, nm, typ, ttl, val))
            i += ln
    return {"rcode": RCODE.get(flags & 0xF, str(flags & 0xF)), "aa": bool(flags & 0x0400), "recs": recs}


def main(domain):
    print(f"== DNS check for {domain} ==")
    tld = domain.rsplit(".", 1)[1]
    root = query("198.41.0.4", tld + ".", 2, rd=False)                    # a.root-servers.net
    tld_ns = [(n, v) for s, n, t, ttl, v in root["recs"] if s == "ar" and t == 1][:4]
    print(f"registry (.{tld}) servers: {', '.join(n for n, _ in tld_ns) or 'not found'}")
    deleg = set()
    for name, ip in tld_ns[:2]:
        r = query(ip, domain, 2, rd=False)
        ns = sorted({v for s, n, t, ttl, v in r["recs"] if t == 2})
        deleg.update(ns)
        print(f"  {name}: {r['rcode']}, nameservers {', '.join(ns) or 'NONE'}")
    if not deleg:
        print("  THE REGISTRY DOES NOT DELEGATE THIS DOMAIN (expired, on hold, or unverified registrant): every visitor gets NXDOMAIN")
    print("authoritative nameservers, asked directly (5 times each):")
    for ns in sorted(deleg):
        try:
            ips = sorted({x[4] for x in query("8.8.8.8", ns, 1)["recs"] if x[2] == 1})
        except OSError as e:
            print(f"  {ns}: cannot resolve its address ({e})")
            continue
        for ip in ips[:2]:
            res = []
            for _ in range(5):
                try:
                    r = query(ip, domain, 1, rd=False)
                    a = sorted(v for s, n, t, ttl, v in r["recs"] if s == "an" and t == 1)
                    res.append(f"{r['rcode']}{'' if r['aa'] else '(not authoritative)'} {','.join(a) or '-'}")
                except OSError as e:
                    res.append(f"no answer ({e.__class__.__name__})")
            print(f"  {ns} [{ip}]: " + " | ".join(sorted(set(res))) + f"  ({len(res)} tries)")
    print("public resolvers (5 times each, A record):")
    for name, ip in PUBLIC.items():
        out = {}
        for _ in range(5):
            try:
                r = query(ip, domain, 1)
                a = sorted(v for s, n, t, ttl, v in r["recs"] if s == "an" and t == 1)
                k = f"{r['rcode']} {','.join(a) or '-'}"
            except OSError as e:
                k = f"no answer ({e.__class__.__name__})"
            out[k] = out.get(k, 0) + 1
        print(f"  {name:24s} " + " | ".join(f"{k} x{v}" for k, v in out.items()))
    try:                                           # negative caching and DNSSEC
        ip = socket.gethostbyname(sorted(deleg)[0])
        r = query(ip, domain, 6, rd=False)
        soa = [x for x in r["recs"] if x[2] == 6]
        if soa:
            raw = query_raw(ip, domain, 6)
            print(f"SOA: ttl {soa[0][3]} s, negative-cache (minimum) {raw} s")
        r = query(tld_ns[0][1], domain, 43, rd=False)
        print(f"DNSSEC DS at the registry: {'yes' if any(x[2] == 43 for x in r['recs']) else 'no (unsigned)'}")
    except OSError as e:
        print(f"SOA / DS not checked ({e})")
    for sub in ("www." + domain,):
        try:
            r = query("8.8.8.8", sub, 1)
            print(f"{sub}: {r['rcode']} " + ", ".join(f"{t == 5 and 'CNAME ' or ''}{v}" for s, n, t, ttl, v in r["recs"] if s == "an"))
        except OSError as e:
            print(f"{sub}: no answer ({e})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "beesells.xyz")
