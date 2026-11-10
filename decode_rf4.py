# -*- coding: utf-8 -*-
"""RF4 private-server protocol decoder.

pcapng -> TCP reassembly -> handshake detect -> RC4 decrypt -> frame/RPC dump.
Usage: python decode_rf4.py <capture.pcapng>
Stdlib only.
"""
import sys, struct, json, binascii


def parse_pcapng(path):
    data = open(path, 'rb').read()
    i = 0
    ifaces = []
    packets = []
    while i + 12 <= len(data):
        btype, blen = struct.unpack_from('<II', data, i)
        if blen < 12 or i + blen > len(data):
            break
        body = data[i + 8:i + blen - 4]
        if btype == 1:  # IDB
            lt = struct.unpack_from('<H', body, 0)[0] if len(body) >= 8 else 1
            ifaces.append(lt)
        elif btype == 6:  # EPB
            if len(body) >= 20:
                ifid, ts_hi, ts_lo, caplen, origlen = struct.unpack_from('<IIIII', body, 0)
                packets.append(((ts_hi << 32) | ts_lo, ifid, body[20:20 + caplen]))
        elif btype == 3:  # SPB
            if len(body) >= 4:
                origlen = struct.unpack_from('<I', body, 0)[0]
                packets.append((0, 0, body[4:4 + origlen]))
        i += blen
    return ifaces, packets


def l3_from_link(pkt, lt):
    if lt == 1:
        if len(pkt) < 14:
            return None
        et = struct.unpack_from('>H', pkt, 12)[0]
        off = 14
        while et in (0x8100, 0x88a8) and len(pkt) >= off + 4:
            et = struct.unpack_from('>H', pkt, off + 2)[0]
            off += 4
        return pkt[off:] if et == 0x0800 else None
    if lt == 101:
        return pkt
    if lt == 113:
        return pkt[16:]
    return None


def parse_ip_udp(ip):
    if len(ip) < 20 or (ip[0] >> 4) != 4 or ip[9] != 17:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 8:
        return None
    total = struct.unpack_from('>H', ip, 2)[0]
    src = '.'.join(str(b) for b in ip[12:16])
    dst = '.'.join(str(b) for b in ip[16:20])
    udp = ip[ihl:total] if total <= len(ip) else ip[ihl:]
    if len(udp) < 8:
        return None
    sport, dport, ulen = struct.unpack_from('>HHH', udp, 0)
    return src, sport, dst, dport, udp[8:ulen] if ulen >= 8 else udp[8:]


def parse_ip_tcp(ip):
    if len(ip) < 20 or (ip[0] >> 4) != 4 or ip[9] != 6:
        return None
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 20:
        return None
    total = struct.unpack_from('>H', ip, 2)[0]
    src = '.'.join(str(b) for b in ip[12:16])
    dst = '.'.join(str(b) for b in ip[16:20])
    tcp = ip[ihl:total] if total <= len(ip) else ip[ihl:]
    if len(tcp) < 20:
        return None
    sport, dport, seq = struct.unpack_from('>HHI', tcp, 0)
    doff = (tcp[12] >> 4) * 4
    return src, sport, dst, dport, seq, tcp[doff:]


def reassemble(packets, ifaces):
    flows = {}
    global UDP_FLOWS
    UDP_FLOWS = {}
    for ts, ifid, pkt in packets:
        lt = ifaces[ifid] if ifid < len(ifaces) else 1
        ip = l3_from_link(pkt, lt)
        if not ip:
            continue
        u = parse_ip_udp(ip)
        if u:
            src, sport, dst, dport, payload = u
            if payload:
                UDP_FLOWS.setdefault((src, sport, dst, dport), []).append(payload)
            continue
        r = parse_ip_tcp(ip)
        if not r:
            continue
        src, sport, dst, dport, seq, payload = r
        if payload:
            flows.setdefault((src, sport, dst, dport), []).append((seq, payload))
    out = {}
    for key, segs in flows.items():
        segs.sort(key=lambda x: x[0])
        base = segs[0][0]
        buf = bytearray()
        for seq, pay in segs:
            off = seq - base
            if off < 0:
                continue
            if off > len(buf):
                buf.extend(b'\x00' * (off - len(buf)))
            end = off + len(pay)
            if end <= len(buf):
                continue
            buf.extend(pay[len(buf) - off:])
        out[key] = bytes(buf)
    return out


class RC4:
    def __init__(self, key):
        S = list(range(256))
        j = 0
        for i in range(256):
            j = (j + S[i] + key[i % len(key)]) & 0xFF
            S[i], S[j] = S[j], S[i]
        self.S, self.i, self.j = S, 0, 0

    def process(self, data):
        S, i, j = self.S, self.i, self.j
        out = bytearray(len(data))
        for k, b in enumerate(data):
            i = (i + 1) & 0xFF
            j = (j + S[i]) & 0xFF
            S[i], S[j] = S[j], S[i]
            out[k] = b ^ S[(S[i] + S[j]) & 0xFF]
        self.i, self.j = i, j
        return bytes(out)


def try_handshake(buf):
    if len(buf) < 6:
        return None
    enc, flag = buf[0], buf[1]
    n = struct.unpack_from('<I', buf, 2)[0]
    if enc not in (0, 1) or flag not in (0, 1) or n > 4096 or len(buf) < 6 + n:
        return None
    token = buf[6:6 + n]
    if n and not all(32 <= c < 127 for c in token):
        return None
    return enc, flag, token


def printable(b, limit=160):
    return ''.join(chr(c) if 32 <= c < 127 else '.' for c in b[:limit])


def dump_frames(buf, tag):
    off = 0
    n = 0
    while off + 4 <= len(buf):
        ln = struct.unpack_from('<I', buf, off)[0]
        if ln < 5 or ln > 10485760 or off + 4 + ln > len(buf):
            break
        payload = buf[off + 4:off + 4 + ln]
        typ = payload[0]
        seq = struct.unpack_from('<I', payload, 1)[0] if len(payload) >= 5 else -1
        data = payload[5:]
        n += 1
        txt = ''
        if data[:1] in (b'{', b'['):
            try:
                txt = json.dumps(json.loads(data.decode('utf-8', 'replace')), ensure_ascii=False)[:400]
            except Exception:
                txt = printable(data, 120)
        else:
            txt = printable(data, 120)
        print('%s #%d type=%d seq=%d len=%d %s' % (tag, n, typ, seq, ln, txt))
        off += 4 + ln
    if off < len(buf):
        print('%s trailing %d bytes: %s' % (tag, len(buf) - off, printable(buf[off:], 200)))
    return n


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    ifaces, packets = parse_pcapng(sys.argv[1])
    print('packets=%d ifaces=%s' % (len(packets), ifaces))
    flows = reassemble(packets, ifaces)
    print('flows=%d' % len(flows))
    hs = None
    for key, buf in flows.items():
        h = try_handshake(buf)
        if h:
            hs = (key, buf, h)
            break
    if UDP_FLOWS:
        print('UDP flows=%d' % len(UDP_FLOWS))
        for key, dgrams in sorted(UDP_FLOWS.items(), key=lambda kv: -sum(len(d) for d in kv[1]))[:12]:
            total = sum(len(d) for d in dgrams)
            first = b''.join(dgrams[:3])
            print('  UDP %-40s pkt=%d bytes=%d %s' % ('%s:%d -> %s:%d' % key, len(dgrams), total, printable(first, 60)))
    if not hs:
        print('!! no handshake found. Top TCP flows by size:')
        for key, buf in sorted(flows.items(), key=lambda kv: -len(kv[1]))[:20]:
            print('  TCP %-40s %9d %s' % ('%s:%d -> %s:%d' % key, len(buf), printable(buf, 80)))
        return
    key, buf, (enc, flag, token) = hs
    print('handshake flow=%s enc=%d flag=%d token=%s' % (key, enc, flag, token.decode('latin1')))
    rev = (key[2], key[3], key[0], key[1])
    c2s = buf[6 + len(token):]
    s2c = flows.get(rev, b'')
    print('c2s=%d s2c=%d' % (len(c2s), len(s2c)))
    if enc and token:
        c2s_p = RC4(token).process(c2s)
        s2c_p = b''
        if len(s2c) >= 16:
            print('server hello:', binascii.hexlify(s2c[:16]).decode())
            s2c_p = RC4(token).process(s2c[16:])
    else:
        c2s_p, s2c_p = c2s, s2c[16:]
    print('\n--- c2s first 200 ---\n' + printable(c2s_p, 200))
    print('\n--- s2c first 200 ---\n' + printable(s2c_p, 200))
    print('\n--- C2S frames ---')
    dump_frames(c2s_p, 'C2S')
    print('\n--- S2C frames ---')
    dump_frames(s2c_p, 'S2C')


if __name__ == '__main__':
    main()
