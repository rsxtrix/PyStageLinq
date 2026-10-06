#!/usr/bin/env python3
"""
Minimal StageLinq listener for Denon Prime: prints state changes as plain text.

    python3 stagelinq.py [interface] [-d] [--new-token] [--np]

    interface    network interface the Prime is plugged into (default: eth0)
    -d           also hex-dump raw StateMap bytes (for debugging)
    --new-token  discard the saved client token and generate a new one
    --np         identify as the "Now Playing" client the Node library uses
                 (fallback in case a device rejects the default identity)

UNOFFICIAL: StageLinq is a reverse-engineered protocol, not supported by Denon,
and a firmware update could change it. Developed and tested against a Denon
Prime 4 running firmware 5.0.4 only.

Credit: message formats were worked out from the community documentation and
behaviour of chrisle/StageLinq (https://github.com/chrisle/StageLinq), checked
against a packet capture of that library talking to a Prime 4.

Client token: nothing needs to be registered or requested. On first run this
script generates its own random 16-byte token and keeps it in ~/.stagelinq_token,
so every install has a unique identity and two clients on one network never
clash. (A Prime 4 on firmware 5.0.4 accepted random tokens in testing; other
devices and firmware versions are untested.)
"""
import fcntl
import json
import os
import socket
import struct
import sys
import threading
import time

ARGS = [a for a in sys.argv[1:] if not a.startswith("-")]
IFACE = ARGS[0] if ARGS else "eth0"
DEBUG = "-d" in sys.argv

DISCOVERY_PORT = 51337
ACTION = "DISCOVERER_HOWDY_"
TOKEN_FILE = os.path.expanduser("~/.stagelinq_token")


def load_token(force_new=False):
    """A 16-byte random client ID, generated once per install and then reused.

    Tested against a Prime 4: random tokens are accepted, so there is nothing
    to register or request. If the file is missing, damaged, or can't be
    written, we fall back to a fresh random token for this run.
    """
    if not force_new:
        try:
            with open(TOKEN_FILE, "rb") as f:
                t = f.read()
                if len(t) == 16:
                    return t
        except OSError:
            pass
    t = os.urandom(16)
    try:
        with open(TOKEN_FILE, "wb") as f:
            f.write(t)
        os.chmod(TOKEN_FILE, 0o600)
    except OSError as e:
        print("Warning: could not save token to %s (%s); using a temporary one" % (TOKEN_FILE, e))
    return t


if "--np" in sys.argv:
    # Exactly what the Node library's "Now Playing" client announced in the capture.
    TOKEN = bytes.fromhex("52fdfc072182654f163f5f0f9a621d72")
    SOURCE, SW_NAME, SW_VERSION = "np2", "nowplaying", "2.2.0"
else:
    TOKEN = load_token(force_new="--new-token" in sys.argv)
    SOURCE = socket.gethostname()[:32]
    SW_NAME, SW_VERSION = "stagelinq-py", "0.1.0"


def _opt(name):
    for a in sys.argv[1:]:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


# Per-field overrides, for working out which part of the identity a device checks
_tok = _opt("--token")
if _tok == "random":
    TOKEN = load_token()
elif _tok:
    try:
        TOKEN = bytes.fromhex(_tok)
    except ValueError:
        TOKEN = b""
    if len(TOKEN) != 16:
        sys.exit("--token must be 32 hex characters (16 bytes)")
SOURCE = _opt("--source") or SOURCE
SW_NAME = _opt("--name") or SW_NAME
SW_VERSION = _opt("--sw-version") or SW_VERSION

# States to subscribe to. /Mixer/CH<n>faderPosition is confirmed by the capture;
# the /Engine paths are from memory of the library docs (docs/statemap.md).
STATES = ["/Mixer/CrossfaderPosition"]
for _n in range(1, 5):
    STATES.append("/Mixer/CH%dfaderPosition" % _n)
    STATES += ["/Engine/Deck%d/%s" % (_n, s) for s in (
        "Play", "PlayState", "CurrentBPM", "Speed", "DeckIsMaster",
        "Track/SongLoaded", "Track/SongName", "Track/ArtistName",
        "Track/CurrentBPM", "Track/TrackNetworkPath")]


# ---------- byte helpers ----------
class Short(Exception):
    """Buffer ends before the message does; wait for more data."""


def enc_str(s):
    b = s.encode("utf-16-be")
    return struct.pack(">I", len(b)) + b


def need(buf, pos, n):
    if pos + n > len(buf):
        raise Short


def read_str(buf, pos):
    need(buf, pos, 4)
    (n,) = struct.unpack_from(">I", buf, pos)
    need(buf, pos + 4, n)
    return buf[pos + 4:pos + 4 + n].decode("utf-16-be"), pos + 4 + n


def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data += chunk
    return data


def iface_addr(ifname, request):
    """Linux ioctl: 0x8915 = interface IP, 0x8919 = broadcast address."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        r = fcntl.ioctl(s.fileno(), request,
                        struct.pack("256s", ifname.encode()[:15]))
    return socket.inet_ntoa(r[20:24])


# ---------- discovery ----------
def build_announcement():
    return (b"airD" + TOKEN + enc_str(SOURCE) + enc_str(ACTION)
            + enc_str(SW_NAME) + enc_str(SW_VERSION) + struct.pack(">H", 0))


def announce_loop(local_ip, bcast):
    # Bound to the Ethernet address and sent to its broadcast address, so the
    # packets leave via the cable and not via Wi-Fi.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind((local_ip, 0))
    msg = build_announcement()
    while True:
        s.sendto(msg, (bcast, DISCOVERY_PORT))
        time.sleep(1)


def parse_discovery(data):
    if data[:4] != b"airD":
        return None
    token = data[4:20]
    _source, pos = read_str(data, 20)
    _action, pos = read_str(data, pos)
    software, pos = read_str(data, pos)
    version, pos = read_str(data, pos)
    need(data, pos, 2)
    (port,) = struct.unpack_from(">H", data, pos)
    return token, software, version, port


# ---------- Directory service ----------
def find_statemap_port(d):
    """Read directory messages until the StateMap service is announced.

    id 0 = service announcement (token, name, port)
    id 1 = reference/time message (44 bytes)
    id 2 = services request (token)
    """
    buf = b""
    while True:
        try:
            need(buf, 0, 4)
            (mid,) = struct.unpack_from(">I", buf, 0)
            if mid == 2:
                end = 20
                need(buf, 0, end)
            elif mid == 1:
                end = 44
                need(buf, 0, end)
            elif mid == 0:
                need(buf, 0, 20)
                name, pos = read_str(buf, 20)
                need(buf, pos, 2)
                (port,) = struct.unpack_from(">H", buf, pos)
                end = pos + 2
                if name == "StateMap":
                    return port
            else:
                raise ValueError("unknown directory message id %d" % mid)
            buf = buf[end:]
        except Short:
            chunk = d.recv(4096)
            if not chunk:
                raise ConnectionError("directory connection closed")
            buf += chunk


# ---------- StateMap service ----------
def show(name, raw):
    try:
        obj = json.loads(raw)
        val = obj
        if isinstance(obj, dict):
            val = next((obj[k] for k in ("value", "string", "state") if k in obj), obj)
    except (ValueError, TypeError):
        val = raw
    print("%s  %s = %s" % (time.strftime("%H:%M:%S"), name, val), flush=True)


def drain(buf):
    """Pull complete JSON state messages out of buf. Returns (events, leftover)."""
    events = []
    while True:
        i = buf.find(b"smaa")
        if i < 0:
            return events, buf[-3:]
        try:
            need(buf, i + 4, 4)
            (mtype,) = struct.unpack_from(">I", buf, i + 4)
            name, pos = read_str(buf, i + 8)
            if mtype == 0:  # JSON state update
                raw, pos = read_str(buf, pos)
                events.append((name, raw))
            buf = buf[pos:]
        except Short:
            return events, buf[i:]


def handle_statemap(ip, port):
    s = socket.create_connection((ip, port), timeout=10)
    s.settimeout(None)
    local_port = s.getsockname()[1]
    # Service init: id 0, our token, service name, our local port
    s.sendall(struct.pack(">I", 0) + TOKEN + enc_str("StateMap")
              + struct.pack(">H", local_port))
    # One length-prefixed "smaa" subscribe message per state
    for path in STATES:
        body = b"smaa" + struct.pack(">I", 0x000007D2) + enc_str(path) + struct.pack(">I", 0)
        s.sendall(struct.pack(">I", len(body)) + body)
    print("[StateMap] subscribed to %d states\n" % len(STATES), flush=True)

    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            print("[StateMap] connection closed")
            return
        if DEBUG:
            print("[raw] " + chunk.hex(), flush=True)
        events, buf = drain(buf + chunk)
        for name, raw in events:
            show(name, raw)


def run_statemap(ip, port):
    try:
        handle_statemap(ip, port)
    except Exception as e:
        print("[StateMap error] %s" % e)


def handle_device(ip, port):
    print("[Directory] connecting to %s:%d" % (ip, port), flush=True)
    d = socket.create_connection((ip, port), timeout=5)
    try:
        # The Prime speaks first: id 2 + its token. Wait for it, then answer
        # with our own services request (this is the order the working client used).
        try:
            recv_exact(d, 20)
        except socket.timeout:
            pass
        d.sendall(struct.pack(">I", 2) + TOKEN)

        sm_port = find_statemap_port(d)
        print("[Directory] StateMap service on port %d" % sm_port, flush=True)
        threading.Thread(target=run_statemap, args=(ip, sm_port), daemon=True).start()

        d.settimeout(None)  # keep the directory connection open
        while d.recv(4096):
            pass
    finally:
        d.close()


def run_device(ip, port, connected):
    try:
        handle_device(ip, port)
    except Exception as e:
        print("[Error] %s" % e)
        print("        If this keeps happening, try --new-token (fresh client ID), then --np.")
    finally:
        connected.discard((ip, port))  # allow reconnect on next announcement


def main():
    try:
        local_ip = iface_addr(IFACE, 0x8915)
        bcast = iface_addr(IFACE, 0x8919)
    except OSError as e:
        sys.exit("Cannot read the address of %s: %s" % (IFACE, e))
    print("Using %s: %s (broadcast %s)" % (IFACE, local_ip, bcast))

    threading.Thread(target=announce_loop, args=(local_ip, bcast), daemon=True).start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    print("Listening for StageLinq devices...", flush=True)

    connected = set()
    while True:
        data, (ip, _) = sock.recvfrom(4096)
        try:
            info = parse_discovery(data)
        except (Short, UnicodeDecodeError):
            continue
        if not info:
            continue
        token, software, version, port = info
        # Skip ourselves, port-less entries, and the Prime's background
        # OfflineAnalyzer process (the real device is the other entry).
        if token == TOKEN or port == 0 or software == "OfflineAnalyzer":
            continue
        key = (ip, port)
        if key in connected:
            continue
        connected.add(key)
        print("Found %s %s at %s:%d" % (software, version, ip, port), flush=True)
        threading.Thread(target=run_device, args=(ip, port, connected), daemon=True).start()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
