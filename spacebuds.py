#!/usr/bin/env python3
"""spacebuds.py — Ubuntu CLI for oraimo SpaceBuds (JieLi RCSP over BLE).

Protocol reverse-engineered from oraimo-sound 1.3.2 (see PORT.md):
  transport : BLE GATT service 0000ae00-… — write  …ae02, notify …ae01
  framing   : FE DC BA | flags | op | len:2BE | sn | params | EF
  opcodes   : GetSysInfo = 7, SetSysInfo = 8 (function byte 0xFF)
  attrs     : 13 = noise mode (0 off / 1 ANC / 2 transparency),
              4 = EQ data, 12 = EQ preset
  get voice : GetSysInfo(FF, mask 8192)      set voice : SetSysInfo(FF, [13:9B])
  get EQ    : GetSysInfo(FF, mask 4112)

First run needs one user action: put the buds in pairing mode
(open the case lid), then:  spacebuds.py pair
After that the address is cached in ~/.cache/spacebuds.json.

Requires: pip install --user bleak  (BlueZ 5.85 + PipeWire tested).
Read-only commands (status/eq/battery) only query; `anc` changes the
buds' noise mode, exactly like the Android app does.
"""

import asyncio
import json
import os
import struct
import subprocess
import sys
import time

CLASSIC_MAC = "82:08:20:02:69:97"
CACHE = os.path.expanduser("~/.cache/spacebuds.json")

SVC = "0000ae00-0000-1000-8000-00805f9b34fb"
CHR_NOTIFY = "0000ae01-0000-1000-8000-00805f9b34fb"
CHR_WRITE = "0000ae02-0000-1000-8000-00805f9b34fb"

MAGIC = b"\xfe\xdc\xba"
OP_GET_SYSINFO = 7
OP_SET_SYSINFO = 8
FUNC_SYS = 0xFF
ATTR_NOISE_MODE = 13
ATTR_EQ = 4
ATTR_EQ_PRESET = 12
MASK_VOICE_MODE = 8192
MASK_EQ = 4112

MODES = {"off": 0, "anc": 1, "denoise": 1, "transparency": 2,
         "transparent": 2}
MODE_NAMES = {0: "off", 1: "ANC", 2: "transparency"}

INTRO_FRAMES = [
    r"""
     (("))          SpaceBuds CLI
    /|___|\         ((o))
    | ___ |       ___|___
    |/   \|      |/     \|
    """,
    r"""
     (((")))        SpaceBuds CLI
    /|___|\       ((  o  ))
    | ___ |       ___|___
    |/   \|      |/     \|
    """,
    r"""
     ((( ")))       SpaceBuds CLI
    /|___|\     ((   o   ))
    | ___ |       ___|___
    |/   \|      |/     \|
    """,
]


def intro() -> None:
    if not sys.stdout.isatty():
        return
    for frame in INTRO_FRAMES:
        sys.stdout.write("\033[H\033[J" + frame + "\n")
        sys.stdout.flush()
        time.sleep(0.18)
    time.sleep(0.3)
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def pack(op: int, sn: int, params: bytes) -> bytes:
    return (MAGIC + bytes([0xC0, op]) + struct.pack(">H", 1 + len(params))
            + bytes([sn]) + params + b"\xef")


def split_attrs(data: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i + 2 <= len(data):
        ln, typ = data[i], data[i + 1]
        if ln < 1 or i + 1 + ln > len(data):
            break
        out.append((typ, data[i + 2:i + 1 + ln]))
        i += 1 + ln
    return out


def load_cache() -> dict:
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return {}


def save_cache(d: dict) -> None:
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(d, open(CACHE, "w"))


class Buds:
    def __init__(self, addr: str) -> None:
        self.addr = addr
        self.sn = 0
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buf = bytearray()

    def _on_notify(self, _h: int, data: bytearray) -> None:
        self._buf += data
        while True:
            i = bytes(self._buf).find(MAGIC)
            if i < 0:
                self._buf.clear()
                return
            if len(self._buf) < i + 7:
                return
            plen = struct.unpack(">H", bytes(self._buf[i + 4:i + 6]))[0]
            end = i + 7 + plen + 1
            if len(self._buf) < end:
                return
            pkt = bytes(self._buf[i:end])
            del self._buf[:end]
            if pkt[-1] == 0xEF:
                self._queue.put_nowait(pkt)

    async def transact(self, client, op: int, params: bytes,
                       timeout: float = 8.0) -> bytes:
        for _ in range(3):  # drain stale packets
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        await client.write_gatt_char(CHR_WRITE, pack(op, self.sn, params),
                                     response=False)
        want = self.sn
        self.sn = (self.sn + 1) & 0xFF
        try:
            raw = await asyncio.wait_for(self._queue.get(), timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise TimeoutError(
                "buds silent (no RCSP reply in "
                f"{timeout:g}s — LE control link up?)") from e
        if raw[:3] != MAGIC or raw[3] & 0x80:
            raise ValueError("bad response framing")
        status, sn = raw[7], raw[8]
        plen = struct.unpack(">H", raw[5:7])[0]
        if sn != want:
            raise ValueError(f"sn mismatch (want {want}, got {sn})")
        if status != 0:
            raise RuntimeError(f"buds returned status {status}")
        return raw[9:7 + plen]

    async def get_sysinfo(self, client, mask: int):
        data = await self.transact(
            client, OP_GET_SYSINFO, bytes([FUNC_SYS]) + struct.pack(">I", mask))
        return split_attrs(data)

    async def get_voice_mode(self, client) -> tuple[int, bytes]:
        for typ, val in await self.get_sysinfo(client, MASK_VOICE_MODE):
            if typ == ATTR_NOISE_MODE and len(val) >= 1:
                return val[0], val
        raise RuntimeError("buds did not report noise mode (attr 13)")

    async def set_voice_mode(self, client, mode: int) -> None:
        _, current = await self.get_voice_mode(client)
        blob = bytes([mode]) + bytes(current[1:9]).ljust(8, b"\x00")
        attr = bytes([len(blob) + 1, ATTR_NOISE_MODE]) + blob
        await self.transact(client, OP_SET_SYSINFO, bytes([FUNC_SYS]) + attr)


async def discover_devices(retries: int = 3):
    """One-shot LE discovery, tolerating BlueZ's concurrent scans."""
    from bleak import BleakScanner
    from bleak.exc import BleakDBusError
    last: Exception | None = None
    for _ in range(retries):
        try:
            return await BleakScanner.discover(timeout=15.0)
        except BleakDBusError as e:
            last = e
            await asyncio.sleep(3.0)
    raise RuntimeError(f"LE scan failed ({last}); retry in a moment")


async def find_buds() -> str:
    """Return LE address of the buds: cache, else scan (tail-match/classic)."""
    cached = load_cache().get("le_addr")
    tail = CLASSIC_MAC.replace(":", "")[-8:].lower()

    devs = await discover_devices()
    if cached and any(d.address == cached for d in devs):
        return cached
    classic = CLASSIC_MAC.upper()
    tail_hits = [d for d in devs
                 if d.address.replace(":", "")[-8:].lower() == tail
                 and d.address.upper() != classic]
    name_hits = [d for d in devs
                 if d.name and "spacebuds" in d.name.lower()
                 and d.address.upper() != classic]
    # Prefer the LE identity over the classic MAC: GATT-over-BR/EDR needs
    # the classic link up, while the paired LE bond works standalone.
    for d in tail_hits + name_hits:
        return d.address
    raise RuntimeError("buds not advertising; open the case lid and retry")


async def run_session(addr: str, fn):
    from bleak import BleakClient
    buds = Buds(addr)
    try:
        async with BleakClient(addr, timeout=12.0) as client:
            svcs = [s.uuid.lower() for s in client.services]
            if SVC not in svcs:
                raise RuntimeError(f"no ae00 control service on {addr}")
            await client.start_notify(CHR_NOTIFY, buds._on_notify)
            try:
                return await fn(buds, client)
            finally:
                try:
                    await client.stop_notify(CHR_NOTIFY)
                except Exception:
                    pass
    except TimeoutError as e:
        raise ConnectionError(
            f"LE connect to {addr} timed out (buds not answering — "
            f"open the lid / connect the LE entry in GNOME)") from e
    except (RuntimeError, ValueError):
        raise
    except Exception as e:
        raise ConnectionError(
            f"LE link to {addr} failed ({e}); connect the LE entry in "
            f"GNOME's panel and retry") from e


def bt_battery() -> str:
    try:
        out = subprocess.run(["bluetoothctl", "info", CLASSIC_MAC],
                             capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "Battery Percentage" in line:
                return line.split("(")[-1].rstrip(")")
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


async def cmd_status() -> int:
    async def fn(buds, client):
        mode, raw = await buds.get_voice_mode(client)
        print(f"noise mode : {MODE_NAMES.get(mode, mode)} (raw {raw.hex()})")
        print(f"battery    : {bt_battery()} (via BlueZ)")
    await run_session(await find_buds(), fn)
    return 0


async def cmd_anc(arg: str) -> int:
    key = arg.lower()
    if key not in MODES:
        print("usage: spacebuds.py anc [off|anc|transparency]",
              file=sys.stderr)
        return 2

    async def fn(buds, client):
        await buds.set_voice_mode(client, MODES[key])
        mode, _ = await buds.get_voice_mode(client)
        print(f"noise mode : {MODE_NAMES.get(mode, mode)}")
    await run_session(await find_buds(), fn)
    return 0


async def cmd_eq() -> int:
    async def fn(buds, client):
        for typ, val in await buds.get_sysinfo(client, MASK_EQ):
            if typ == ATTR_EQ_PRESET:
                print(f"EQ preset  : {val.hex()} "
                      f"(int={int.from_bytes(val, 'big')})")
            elif typ == ATTR_EQ:
                print(f"EQ data    : {val.hex()}")
            else:
                print(f"attr {typ:<3}: {val.hex()}")
    await run_session(await find_buds(), fn)
    return 0


async def cmd_pair() -> int:
    """Use (don't manage) the OS connection: find the buds' LE identity,
    verify the ae00 control service, cache the address.

    Pairing/connecting is GNOME's job (top-bar Bluetooth panel — connect
    BOTH SpaceBuds entries). This command only verifies and caches.
    """
    print("make sure BOTH SpaceBuds entries are connected in GNOME's "
          "Bluetooth panel, then this verifies the control channel…")
    devs = await discover_devices()
    tail = CLASSIC_MAC.replace(":", "")[-8:].lower()
    cands = [d for d in devs
             if (d.name and "spacebuds" in d.name.lower())
             or d.address.replace(":", "")[-8:].lower() == tail]
    cands = [d for d in cands if d.address.upper() != CLASSIC_MAC.upper()]
    cached = load_cache().get("le_addr")
    if cached:
        cands = sorted(cands, key=lambda d: d.address != cached)
    if not cands:
        print("buds not found advertising; open the lid and retry",
              file=sys.stderr)
        return 1
    for d in cands:
        print(f"verifying ae00 control on {d.address} ({d.name})…")
        try:
            async def fn(buds, client):
                return True
            await run_session(d.address, fn)
            save_cache({"le_addr": d.address})
            print(f"control channel live on {d.address}")
            return 0
        except Exception as e:
            print(f"not yet: {e}", file=sys.stderr)
    print("no control channel — connect both entries in GNOME's panel "
          "and re-run", file=sys.stderr)
    return 1


def decode_spp_tlv(blob: bytes) -> list[tuple[int, bytes]]:
    """JL SPP link-info framing: [03][type][len:2BE][data] records."""
    out, i = [], 0
    while i + 4 <= len(blob):
        if blob[i] != 0x03:
            i += 1
            continue
        ln = struct.unpack(">H", blob[i + 2:i + 4])[0]
        if i + 4 + ln > len(blob):
            break
        out.append((blob[i + 1], blob[i + 2 + 2:i + 4 + ln]))
        i += 4 + ln
    return out


def cmd_watch(secs: int = 60) -> int:
    """Passive monitor: print everything the buds push on SPP ch10.

    No pairing needed. While it runs, tap/hold a bud (touch gesture
    usually cycles ANC) and watch which record changes — that calibrates
    the live state decode.
    """
    import socket as _socket
    s = _socket.socket(_socket.AF_BLUETOOTH, _socket.SOCK_STREAM,
                       _socket.BTPROTO_RFCOMM)
    s.settimeout(8.0)
    try:
        s.connect((CLASSIC_MAC, 10))
    except OSError:
        # Buds sometimes reset the first SPP attempt (e.g. A2DP active);
        # one immediate retry is enough.
        try:
            s.close()
        except OSError:
            pass
        s = _socket.socket(_socket.AF_BLUETOOTH, _socket.SOCK_STREAM,
                           _socket.BTPROTO_RFCOMM)
        s.settimeout(8.0)
        try:
            s.connect((CLASSIC_MAC, 10))
        except OSError as e:
            print(f"error: SPP ch10 connect failed ({e}); "
                  f"is classic audio connected?", file=sys.stderr)
            return 1
    print(f"listening on SPP ch10 for {secs}s "
          f"(tap/hold a bud to cycle ANC)…")
    s.settimeout(1.0)
    deadline = time.time() + secs
    buf = bytearray()
    try:
        while time.time() < deadline:
            try:
                chunk = s.recv(128)
            except _socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            recs = decode_spp_tlv(bytes(buf))
            if recs and len(buf) >= 4:
                t = time.strftime("%H:%M:%S")
                for typ, val in recs:
                    extra = ""
                    if typ == 0x02 and len(val) == 6:
                        extra = "  (bdaddr %s)" % ":".join(
                            f"{bb:02X}" for bb in val)
                    print(f"{t}  rec type={typ:#04x} len={len(val)} "
                          f"{val.hex()}{extra}")
                buf.clear()
            if len(buf) > 256:
                print(f"{time.strftime('%H:%M:%S')}  "
                      f"raw {bytes(buf).hex()}")
                buf.clear()
    except KeyboardInterrupt:
        pass
    finally:
        s.close()
    return 0


def usage() -> int:
    print("usage: spacebuds.py [--no-intro] "
          "{pair|status|anc [off|anc|transparency]|eq|battery|watch [secs]}")
    return 2


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--no-intro"]
    if "--no-intro" not in argv:
        intro()
    if not args:
        return usage()
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "battery":
            print(f"battery: {bt_battery()}")
            return 0
        if cmd == "pair":
            return asyncio.run(cmd_pair())
        if cmd == "status":
            return asyncio.run(cmd_status())
        if cmd == "anc":
            return asyncio.run(cmd_anc(rest[0] if rest else ""))
        if cmd == "eq":
            return asyncio.run(cmd_eq())
        if cmd == "watch":
            try:
                secs = int(rest[0]) if rest else 60
            except ValueError:
                return usage()
            return cmd_watch(secs)
        return usage()
    except (ConnectionError, TimeoutError, RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # e.g. bleak D-Bus errors
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
