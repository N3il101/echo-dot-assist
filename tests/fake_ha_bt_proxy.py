#!/usr/bin/env python3
"""The Bluetooth proxy switched on the settings page (issue #152), against build/hassmic-host with ble.c talking H4 to
the controller of tests/fake_improv.py on a pseudo terminal (HASSMIC_BT_DEV), which here also scans: on by default,
Home Assistant (aioesphomeapi) finds the proxy in the device info, subscribes, and the controller's advertisements
reach it, and a tag connects; every slot of ble.h's BLE_MAX_CONN takes a tag at once, Home Assistant is told each
address and that none is free, and one tag more is refused with "connection limit exceeded"; switched off, the links close, the scan and the tag's link end, the device info names no
proxy, and subscriptions, slots and connection requests are refused, while Bluetooth audio's entity and the setup from
a phone (Improv advertising) stay; on again, all of it is back."""
import asyncio, os, re, struct, tempfile, threading
from aioesphomeapi import APIClient
from esphome_common import check, until, sync, run
from fake_improv import Controller, CONN

PORTS = (17311, 17312, 17313, 17314, 17315)
FLAGS = 1 | 2 | 4 | 8 | 32 | 64                 # proto_esphome.c BLE_FEATURES
TAG = 0x112233445566                            # a tag near the Echo
OP_SCAN_ENABLE, OP_CONNECT, OP_DISCONNECT = 0x200c, 0x200d, 0x0406
ECHO = "C4:95:00:56:95:C4"                       # the Echo's Bluetooth address
SLOTS = int(re.search(r"#define BLE_MAX_CONN (\d+)", open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src",
                                                                       "hassmic", "ble.h")).read())[1])


class Scanner(Controller):
    """The controller, keeping every command: LE Set Scan Enable says whether it scans.  A connection the Echo asks for
    comes up at once with the address it named as the peer (the first one handle CONN, each other one the next handle),
    its MTU request answered on that link; a disconnect is done at once"""
    def __init__(self):
        self.ops, self.handles = [], {}
        super().__init__()

    def command(self, op, par):
        self.ops.append((op, bytes(par)))
        super().command(op, par)
        if op == OP_CONNECT: self.central(int.from_bytes(par[6:12], "little"))
        elif op == OP_DISCONNECT: self.event(0x05, struct.pack("<BHB", 0, struct.unpack("<H", par[:2])[0], 0x13))

    def central(self, addr):                    # LE Connection Complete, role central
        h = self.handles.setdefault(addr, CONN + len(self.handles))
        self.event(0x3e, bytes([1, 0]) + struct.pack("<HBB", h, 0, 0) + addr.to_bytes(6, "little") + struct.pack("<HHHB", 24, 0, 400, 0))

    def on_att(self, handle, pdu):              # ATT Exchange MTU: 23
        if pdu[:1] == b"\x02":
            l2 = struct.pack("<HH", 3, 4) + b"\x03" + struct.pack("<H", 23)
            self.send(bytes([2]) + struct.pack("<HH", handle | 0x2000, len(l2)) + l2)

    def scanning(self):
        return ([p[0] for op, p in self.ops if op == OP_SCAN_ENABLE] or [0])[-1] == 1

    def advert(self, addr, data, rssi=-60):     # LE Advertising Report: one ADV_IND
        rep = bytes([0x02, 1, 0, 0]) + addr.to_bytes(6, "little") + bytes([len(data)]) + data + struct.pack("b", rssi)
        self.event(0x3e, rep)


ctl = Scanner()
ADV = bytes([2, 0x01, 0x06, 5, 0x09]) + b"Tag1"     # flags, name


async def ha():
    cli = APIClient("127.0.0.1", PORTS[0], None)
    await asyncio.wait_for(cli.connect(login=True), 5)
    return cli


events = []                                    # what Home Assistant heard, in order: ("free", ...) and ("link", ...)


async def proxy_works(cli, what):
    advs, free = [], []
    cli.subscribe_bluetooth_le_raw_advertisements(lambda r: advs.extend(a.address for a in r.advertisements))
    cli.subscribe_bluetooth_connections_free(lambda f, l, a: (free.append((f, l, sorted(a))), events.append(("free", f, l, sorted(a)))))
    check(await until(ctl.scanning), f"{what}: Home Assistant subscribed, the controller scans")
    ctl.advert(TAG, ADV)
    check(await until(lambda: TAG in advs), f"{what}: the controller's advertisement reaches Home Assistant")
    check(await until(lambda: free) and free[0] == (SLOTS, SLOTS, []), f"{what}: {SLOTS} connection slots free of {SLOTS}: {free}")
    return free


async def connect(cli, addr):
    up = []
    def state(c, m, err): up.append((c, m, err)); events.append(("link", addr, c, err))
    try: await cli.bluetooth_device_connect(addr, state, timeout=5, feature_flags=FLAGS, address_type=0)
    except Exception as x: up.append(x)
    return up[:1]


async def main(e):
    check(await until(lambda: "bluetooth: controller ready" in e.log(), 10), "the controller came up")
    check(e.pv("bluetooth_proxy") == 1, "Bluetooth proxy on by default")
    cli = await ha()
    info = await cli.device_info()
    check(info.bluetooth_proxy_feature_flags == FLAGS and info.legacy_bluetooth_proxy_version == 5 and info.bluetooth_mac_address.upper() == ECHO,
          f"device info: the proxy's features ({info.bluetooth_proxy_feature_flags}) and the Bluetooth address ({info.bluetooth_mac_address})")
    free = await proxy_works(cli, "on")
    up = await connect(cli, TAG)
    check(up == [(True, 23, 0)], f"a tag connects through the proxy: {up}")

    # ---- every slot taken: Home Assistant is told each link and that none is left; one more is refused (0x09, HCI's
    # "connection limit exceeded", which ESPHome's proxy answers too)
    tags = [TAG] + [TAG + i for i in range(1, SLOTS)]
    for t in tags[1:]:
        up = await connect(cli, t)
        check(up == [(True, 23, 0)], f"tag {tags.index(t) + 1} of {SLOTS} connects: {up}")
    check(await until(lambda: free[-1] == (0, SLOTS, sorted(tags))), f"all {SLOTS} slots in use, each address told: {free[-1]}")
    check(len({ctl.handles[t] for t in tags}) == SLOTS, f"{SLOTS} links on the controller: {ctl.handles}")
    ops = len(ctl.ops)
    up = await connect(cli, TAG + SLOTS)
    check(up == [(False, 0, 9)], f"one tag more: refused, connection limit exceeded: {up}")
    check(not [op for op, _ in ctl.ops[ops:] if op == OP_CONNECT], "nothing asked of the controller for it")

    # ---- a device ends its link (Govee strips do after 10 s idle): Home Assistant hears it before the slot list leaves
    # it out, ESPHome's order.  The other way round bleak-esphome found a client it still held missing from the list and
    # tore it down as a lost link, "Reconciling stale connection" in its log on every such disconnect.
    n = len(events)
    ctl.event(0x05, struct.pack("<BHB", 0, ctl.handles[tags[1]], 0x13))       # Disconnection Complete, remote user
    check(await until(lambda: free[-1][:2] == (1, SLOTS)), f"its slot is free again: {free[-1]}")
    told = [x for x in events[n:] if x[0] == "free" or x[1] == tags[1]]
    check(told[:1] == [("link", tags[1], False, 0x13)] and told[1:2] == [("free", 1, SLOTS, sorted(tags[:1] + tags[2:]))],
          f"Home Assistant hears the link end first, then the slot list without it: {told}")
    up = await connect(cli, tags[1])
    check(up == [(True, 23, 0)] and await until(lambda: free[-1] == (0, SLOTS, sorted(tags))), f"it connects again into that slot: {up} {free[-1]}")

    # ---- off on the page: the links close, the scan and the tag's link end with the last client
    ops = len(ctl.ops)
    r = e.page.set(bluetooth_proxy="off")
    check(r.get("applied") == 1 and e.conf().get("bluetooth_proxy") == "off", f"switched off on the page, kept in the settings file: {r}")
    check(await until(lambda: not ctl.scanning()), "the links closed: the controller stopped scanning")
    check(await until(lambda: {struct.unpack("<H", p[:2])[0] for op, p in ctl.ops[ops:] if op == OP_DISCONNECT} >= set(ctl.handles.values())),
          f"every tag's link is closed ({len(ctl.handles)})")
    try: await cli.disconnect()
    except Exception: pass
    cli = await ha()
    info = await cli.device_info()
    check(info.bluetooth_proxy_feature_flags == 0 and info.legacy_bluetooth_proxy_version == 0 and not info.bluetooth_mac_address,
          f"device info: no proxy, no Bluetooth address ({info.bluetooth_proxy_feature_flags}, {info.bluetooth_mac_address!r})")
    es, _ = await cli.list_entities_services()
    check(any(x.name == "Bluetooth pairing" for x in es), "Bluetooth audio from phones stays: its \"Bluetooth pairing\" switch is listed")
    check(await until(lambda: ctl.adv["on"], 8), "the setup from a phone stays: Improv advertises (not set up, offline)")
    advs, free = [], []
    cli.subscribe_bluetooth_le_raw_advertisements(lambda r: advs.extend(a.address for a in r.advertisements))
    cli.subscribe_bluetooth_connections_free(lambda f, l, a: free.append((f, l)))
    await sync(cli)
    ops = len(ctl.ops)
    try: await cli.bluetooth_device_connect(TAG, lambda *a: None, timeout=1)
    except Exception: pass
    ctl.advert(TAG, ADV)
    await asyncio.sleep(1.5)                    # what must not happen
    check(not ctl.scanning() and not advs, f"a subscription is refused: no scan, no advertisements ({advs})")
    check(not free, f"connection slots: no answer ({free})")
    check(not [op for op, _ in ctl.ops[ops:] if op in (OP_CONNECT, OP_SCAN_ENABLE)], "a connection request is refused: nothing asked of the controller")
    st, _, text = e.page.call("GET", "/api/export")
    check(st == 200 and "\nbluetooth_proxy=off\n" in text.decode(), "the export has it")

    # ---- on again
    e.page.set(bluetooth_proxy="on")
    try: await cli.disconnect()
    except Exception: pass
    await asyncio.sleep(0.3)
    cli = await ha()
    info = await cli.device_info()
    check(info.bluetooth_proxy_feature_flags == FLAGS and info.bluetooth_mac_address.upper() == ECHO, "on again: the device info names the proxy")
    await proxy_works(cli, "on again")             # all free: the tags' slots were given back
    await cli.disconnect()


mac = tempfile.mktemp()                         # idme's bt_mac: what read_bdaddr takes the address from
open(mac, "w").write(ECHO.replace(":", "").lower() + "\n")
# offline (no such file) and not set up: Improv advertises after 1 s (improv.c's times: not set up, lost, auth, provisioned)
run(main, PORTS, {"HASSMIC_BT_DEV": ctl.path, "HASSMIC_BT_MAC": mac, "HASSMIC_FAKE_ONLINE": mac + ".online",
                  "HASSMIC_IMPROV_TIMES": "1 1 4 2"}, log=True)
