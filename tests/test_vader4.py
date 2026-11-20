#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 zetlaska
#
# SPDX-License-Identifier: MIT

"""Self-test for the Vader 4 Pro old-protocol draft. No hardware: frames and
parsers are checked byte for byte, and the transport runs against a scripted
pad on the far end of a socket pair.

    python3 tests/test_vader4.py
"""
import os
import select
import shutil
import socket
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flydigi import device, vader4  # noqa: E402

TEMP_DIRS = []
PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    if not condition:
        print(f"  FAIL {name} {detail}")


def info_report(device_type=105, battery=3):
    p = bytearray(32)
    p[0] = 0x04
    p[3] = device_type
    p[5:9] = b"\x11\x22\x33\x44"
    p[9], p[10] = 0x23, 0x61        # firmware 6.1.2.3
    p[11], p[12], p[13], p[14] = battery, 1, 2, 1
    p[15] = vader4.D_CMD_INFO
    return bytes(p)


def led_report(index, data):
    p = bytearray(32)
    p[0] = 0x04
    p[3] = index
    p[5:15] = data
    p[15] = vader4.D_CMD_READ_LED
    return bytes(p)


def ack_report(cmd, number):
    p = bytearray(32)
    p[0], p[3], p[15] = 0x04, number, cmd
    return bytes(p)


class ScriptedPad:
    """The far end of a hidraw node. `respond(packet)` returns replies."""

    def __init__(self, respond):
        self.pad, self.node = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.respond = respond
        self.written = []
        self.stop = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop:
            ready, _, _ = select.select([self.pad], [], [], 0.05)
            if not ready:
                continue
            try:
                packet = self.pad.recv(4096)
            except OSError:
                return
            if not packet:
                return
            self.written.append(packet)
            for reply in self.respond(packet) or ():
                self.pad.send(reply)

    def controller(self):
        ctrl = object.__new__(vader4.Controller)
        ctrl.path = "socketpair"
        ctrl.fd = os.dup(self.node.fileno())
        os.set_blocking(ctrl.fd, False)
        ctrl._threads = threading.RLock()
        ctrl._depth = 0
        return ctrl

    def close(self):
        self.stop = True
        self.thread.join(1)
        self.pad.close()
        self.node.close()


def test_frames():
    check("dinput command framing",
          vader4.dinput_command(229, 0) == bytes([5, 229, 0] + [0] * 9))
    x = vader4.xinput_command(16)
    check("xinput command is 15 bytes", len(x) == 15)
    check("xinput checksum is the sum of 0..13", x[14] == (0xA5 + 16) & 0xFF)
    check("xinput args land at 2", vader4.xinput_command(33, 7)[2] == 7)


def test_write_parcels():
    blob = bytes(range(25))
    d = vader4.write_parcels(blob, 1, "dinput", led=True)
    check("dinput: start + 3 parcels", len(d) == 4, len(d))
    check("dinput led start", d[0][:4] == bytes([5, 231, 1, 3]) and len(d[0]) == 14)
    check("dinput parcel 0", d[1] == bytes([5, 51]) + blob[:10] + bytes([0xA0, 0]))
    check("dinput last parcel padded", d[3] == bytes([5, 51]) + blob[20:] + bytes(5) + bytes([0xA0, 2]))
    c = vader4.write_parcels(blob, 2, "dinput", led=False)
    check("dinput config start", c[0][:5] == bytes([5, 234, 3, 0xA0, 2]))
    check("dinput config pack id", c[1][1] == 34)
    x = vader4.write_parcels(blob, 0, "xinput", led=True)
    check("xinput: every packet 15 bytes", all(len(p) == 15 for p in x))
    check("xinput: every packet summed", all(p[14] == sum(p[:14]) & 0xFF for p in x))
    check("xinput led start", x[0][:4] == bytes([0xA5, 42, 0, 3]))
    check("xinput parcel index", x[2][1] == 41 and x[2][12:14] == bytes([0xA0, 1]))
    xc = vader4.write_parcels(blob, 4, "xinput", led=False)
    check("xinput config start", xc[0][:5] == bytes([0xA5, 37, 3, 0xA0, 4]))
    check("500-byte lighting is 50 parcels + start",
          len(vader4.write_parcels(bytes(500), 0)) == 51)
    try:
        vader4.write_parcels(bytes(10), 0, "bluetooth")
        check("unknown mode refused", False)
    except ValueError:
        check("unknown mode refused", True)


def test_parse_dinput():
    r = vader4.parse_dinput(info_report())
    check("info parsed", r is not None and r.kind == "info")
    i = r.info
    check("device type", i.device_type == 105 and i.is_vader4_pro and i.name == "Vader 4 Pro")
    check("firmware string", i.firmware == "6.1.2.3", i.firmware)
    check("mac", i.mac_text == "11:22:33:44")
    check("battery level 3 = 75%", i.battery_percent == 75)
    check("battery as percent", vader4.parse_dinput(info_report(battery=60)).info.battery_percent == 60)
    check("wch, wireless", i.cpu == "wch" and i.connection == "wireless")
    led = vader4.parse_dinput(led_report(4, bytes(range(10))))
    check("led parcel", led.kind == "led" and led.index == 4 and led.data == bytes(range(10)))
    a = vader4.parse_dinput(ack_report(51, 7))
    check("ack", a.kind == "ack" and a.index == 7)
    dongle = vader4.parse_dinput(bytes([4, 17, 0x12, 0x34]) + bytes(28))
    check("dongle", dongle.kind == "dongle" and dongle.data == b"\x12\x34")
    check("input report ignored", vader4.parse_dinput(bytes(32)) is None)
    check("short report ignored", vader4.parse_dinput(b"\x04") is None)
    other = vader4.parse_dinput(info_report(device_type=80)).info
    check("other old-protocol pad named", other.name == "Vader 3 Pro" and not other.is_vader4_pro)


def test_parse_xinput():
    p = bytearray(32)
    p[14], p[15], p[16] = 0xA5, 16, 105
    p[17:21] = b"\xaa\xbb\xcc\xdd"
    p[21], p[22], p[23], p[24], p[25], p[26] = 0x00, 0x61, 100, 1, 1, 2
    r = vader4.parse_xinput(p)
    check("xinput info", r.kind == "info" and r.info.device_type == 105
          and r.info.mode == "xinput" and r.info.connection == "wired")
    p[15], p[16] = 39, 3
    p[17:27] = bytes(range(10, 20))
    r = vader4.parse_xinput(p)
    check("xinput led parcel", r.kind == "led" and r.index == 3 and r.data == bytes(range(10, 20)))
    p[15], p[16] = 41, 9
    check("xinput led ack", vader4.parse_xinput(p) == ("ack", 9, bytes([41]), None))
    p[15] = 42
    check("xinput led start ack is 0", vader4.parse_xinput(p).index == 0)
    p[15], p[16] = 32, 2
    check("xinput config id", vader4.parse_xinput(p).kind == "config_id")
    check("plain xinput input report ignored", vader4.parse_xinput(bytes(20)) is None)


def test_assembler():
    a = vader4.Assembler(3)
    check("out-of-order parcel dropped", not a.add(1, b"x" * 10))
    a.add(0, b"a" * 10)
    a.add(0, b"z" * 10)
    check("duplicate dropped", a.received == 1)
    a.add(1, b"b" * 10)
    check("not done early", a.blob is None and not a.done)
    a.add(2, b"c" * 4)
    check("assembled", a.done and a.blob == b"a" * 10 + b"b" * 10 + b"cccc" + bytes(6))


def test_lighting_codec():
    blob = bytearray(500)
    blob[6], blob[7], blob[8] = 80, 5, vader4.MODE_STEADY
    off = vader4.OFF_FRAMES + (2 * 10 + 0) * 3      # LED 2, frame 0
    blob[off:off + 3] = b"\x01\x02\x03"
    light = vader4.Lighting(blob)
    check("LED-major offsets", light.led(2, 0) == (1, 2, 3))
    check("16 whole LED slots in 500 bytes", light.slots == 16, light.slots)
    light.set_solid((0, 0xE5, 0xFF), brightness=150)
    check("solid keeps length", len(light.blob) == 500)
    check("solid mode", light.mode == vader4.MODE_STEADY and light.led_count == 5)
    check("brightness clamped", light.brightness == 100)
    check("solid frame 0 of LEDs 0..4",
          all(light.led(i, 0) == (0, 0xE5, 0xFF) for i in range(5)))
    check("other frames black", light.led(0, 1) == (0, 0, 0) and light.led(5, 0) == (0, 0, 0))
    check("loop pinned to frame 0", light.loop == (0, 0))
    check("describe", light.describe() == "steady, brightness 100/100 #00E5FF", light.describe())
    light.set_off()
    check("off", light.mode == vader4.MODE_OFF and light.led(0, 0) == (0, 0, 0)
          and light.led_count == 0)
    try:
        light.led(16, 0)
        check("led bound", False)
    except IndexError:
        check("led bound", True)
    try:
        vader4.Lighting(bytes(5))
        check("short blob refused", False)
    except vader4.ProtocolError:
        check("short blob refused", True)


def test_discovery():
    saved = (vader4.glob.glob, device.hid_ids, vader4.interface_number)
    nodes = {"hidraw3": ((0x04B4, 0x2412), 0), "hidraw10": ((0x04B4, 0x2412), 2),
             "hidraw4": ((0x37D7, 0x6001), 0), "hidraw2": ((0x04B4, 0x2412), 2)}
    try:
        vader4.glob.glob = lambda pattern: (["/dev/hidraw" + n[6:] for n in nodes]
                                            if "hidraw" in pattern else [])
        device.hid_ids = lambda node: nodes[node][0]
        vader4.interface_number = lambda node: nodes[node][1]
        found = list(vader4.find_dinput_nodes())
        check("only interface 2 of 04b4:2412, numeric order",
              found == ["/dev/hidraw2", "/dev/hidraw10"], found)
        check("find_device picks the first", vader4.find_device() == "/dev/hidraw2")
        nodes.clear()
        try:
            vader4.find_device()
            check("nothing attached raises", False)
        except device.DeviceNotFound as exc:
            check("nothing attached raises", "DInput" in str(exc))
    finally:
        vader4.glob.glob, device.hid_ids, vader4.interface_number = saved


def _led_stream(blob):
    return [led_report(i, blob[i * 10:(i + 1) * 10]) for i in range(len(blob) // 10)]


def test_transport_reads():
    stored = bytearray(500)
    stored[6], stored[7], stored[8] = 55, 5, vader4.MODE_STEADY
    stored[20:23] = b"\x10\x20\x30"
    noise = bytes(32)                        # an input report, not a reply

    def respond(packet):
        if packet[:2] == bytes([5, vader4.D_CMD_INFO]):
            return [noise, info_report()]
        if packet[:2] == bytes([5, vader4.D_CMD_READ_LED]):
            return [noise] + _led_stream(stored)
        return []
    pad = ScriptedPad(respond)
    try:
        ctrl = pad.controller()
        info = vader4.read_info(ctrl)
        check("read_info over the node", info.device_type == 105)
        check("info request was 12 bytes", pad.written[0] == vader4.dinput_command(236))
        light = vader4.read_lighting(ctrl)
        check("lighting read reassembled", bytes(light.blob) == bytes(stored))
        check("lighting describe", light.describe() == "steady, brightness 55/100 #102030")
        check("require accepts 105", vader4.require(ctrl).is_vader4_pro)
        ctrl.close()
    finally:
        pad.close()


def test_require_refuses_other_pads():
    pad = ScriptedPad(lambda p: [info_report(device_type=80)])
    try:
        ctrl = pad.controller()
        try:
            vader4.require(ctrl)
            check("Vader 3 Pro (80) refused", False)
        except vader4.ProtocolError as exc:
            check("Vader 3 Pro (80) refused", "80" in str(exc))
        ctrl.close()
    finally:
        pad.close()


def test_write_lighting():
    seen = []

    def respond(packet):
        seen.append(packet)
        position = len(seen) - 1
        return [ack_report(packet[1], position)]
    pad = ScriptedPad(respond)
    try:
        ctrl = pad.controller()
        light = vader4.Lighting(bytes(500))
        light.set_solid((255, 0, 0), 80)
        sent = vader4.write_lighting(ctrl, light, wait=0.5)
        check("write sent start + 50 parcels", sent == 51 and len(pad.written) == 51,
              (sent, len(pad.written)))
        check("written bytes are the parcels",
              pad.written == vader4.write_parcels(light.blob, 0, "dinput", led=True))
        ctrl.close()
    finally:
        pad.close()


def test_write_stops_without_ack():
    pad = ScriptedPad(lambda p: [])
    try:
        ctrl = pad.controller()
        try:
            vader4.write_lighting(ctrl, vader4.Lighting(bytes(500)), wait=0.05, retries=2)
            check("silent pad aborts the write", False)
        except vader4.ProtocolError:
            check("silent pad aborts the write", True)
        check("stopped at the start packet, retried once", len(pad.written) == 2,
              len(pad.written))
        ctrl.close()
    finally:
        pad.close()


# -- XInput -------------------------------------------------------------------

class FakeTimeout(Exception):
    pass


class FakeDev:
    """A pyusb device: interface 0 owned by xpad, a scripted vendor channel."""

    def __init__(self, respond, events, fail_attach=False):
        self.respond = respond
        self.events = events
        self.kernel = True
        self.claimed = False
        self.queue = [bytes([0x00, 0x14]) + bytes(18)]     # stray input report
        self.written = []
        self.fail_attach = fail_attach

    def is_kernel_driver_active(self, intf):
        return self.kernel

    def detach_kernel_driver(self, intf):
        self.events.append(("detach", intf))
        self.kernel = False

    def attach_kernel_driver(self, intf):
        self.events.append(("attach", intf))
        if self.fail_attach:
            raise OSError("attach failed")
        self.kernel = True

    def write(self, ep, data, timeout=None):
        assert ep == 0x05 and self.claimed and not self.kernel
        self.written.append(bytes(data))
        self.queue.append(bytes([0x00, 0x14]) + bytes(18))
        self.queue.extend(self.respond(bytes(data)) or ())
        return len(data)

    def read(self, ep, size, timeout=None):
        assert ep == 0x81
        if self.queue:
            return self.queue.pop(0)
        raise FakeTimeout()


class FakeCore:
    USBTimeoutError = FakeTimeout

    def __init__(self, dev):
        self.dev = dev
        self.found = None

    def find(self, **kw):
        self.found = kw
        return self.dev


class FakeUtil:
    def __init__(self, dev, events, fail_claim=False):
        self.dev, self.events, self.fail_claim = dev, events, fail_claim

    def claim_interface(self, dev, intf):
        if self.fail_claim:
            raise OSError("busy")
        self.events.append(("claim", intf))
        dev.claimed = True

    def release_interface(self, dev, intf):
        self.events.append(("release", intf))
        dev.claimed = False

    def dispose_resources(self, dev):
        self.events.append(("dispose",))


def fake_sysfs(product="Flydigi VADER4", vid="045e", pid="028e"):
    root = tempfile.mkdtemp(prefix="vader4-sysfs-")
    TEMP_DIRS.append(root)
    for name, value in (("product", product), ("idVendor", vid), ("idProduct", pid),
                        ("busnum", "10"), ("devnum", "17")):
        with open(os.path.join(root, name), "w") as fh:
            fh.write(value + "\n")
    return root


def x_reply(cmd, payload=b""):
    p = bytearray(32)
    p[0], p[1] = 0x00, 0x14                  # replies ride in input reports
    p[14], p[15] = 0xA5, cmd
    p[16:16 + len(payload)] = payload
    return bytes(p)


def x_info(device_type=105):
    return x_reply(16, bytes([device_type]) + b"\xaa\xbb\xcc\xdd"
                   + bytes([0x00, 0x61, 3, 1, 2, 1]))


def x_pad(respond=None, product="Flydigi VADER4", fail_attach=False, fail_claim=False,
          device_type=105, stored=None):
    events = []
    stored = stored if stored is not None else bytes(500)

    def default(packet):
        if packet[1] == 16:
            return [x_info(device_type)]
        if packet[1] == 38:
            return [x_reply(39, bytes([i]) + stored[i * 10:(i + 1) * 10])
                    for i in range(len(stored) // 10)]
        return []
    dev = FakeDev(respond or default, events, fail_attach)
    sysfs = fake_sysfs(product)
    link = vader4.PyUsbLink(sysfs, FakeCore(dev), FakeUtil(dev, events, fail_claim))
    return dev, events, sysfs, link


def test_xinput_session_detaches_and_reattaches():
    stored = bytearray(500)
    stored[6], stored[7], stored[8] = 70, 5, vader4.MODE_STEADY
    stored[20:23] = b"\x00\xe5\xff"
    dev, events, sysfs, link = x_pad(stored=bytes(stored))
    check("link targets bus/address from sysfs", (link.bus, link.address) == (10, 17))
    with vader4.XInputController(sysfs, link=link) as ctrl:
        check("detached interface 0 only, then claimed",
              events[:2] == [("detach", 0), ("claim", 0)], events)
        check("verified type 105 on open", ctrl.info.is_vader4_pro and ctrl.info.mode == "xinput")
        check("first frame was the info request", dev.written[0] == vader4.xinput_command(16))
        check("xinput frames are summed", dev.written[0][14] == (0xA5 + 16) & 0xFF)
        info = vader4.read_info(ctrl)
        check("read_info in xinput", info.firmware == "6.1.0.0" and info.connection == "wireless",
              (info.firmware, info.connection))
        light = vader4.read_lighting(ctrl)
        check("lighting read in xinput", light.describe() == "steady, brightness 70/100 #00E5FF",
              light.describe())
        check("led read request", dev.written[-1] == vader4.xinput_command(38, 0))
    check("released and reattached on exit",
          events[-3:] == [("release", 0), ("attach", 0), ("dispose",)], events)
    check("xpad owns it again", dev.kernel)
    ctrl.close()
    check("close is idempotent", events.count(("attach", 0)) == 1)


def test_xinput_reattaches_on_errors():
    dev, events, sysfs, link = x_pad(device_type=80)
    try:
        vader4.XInputController(sysfs, link=link)
        check("type 80 over xinput refused", False)
    except vader4.ProtocolError:
        check("type 80 over xinput refused", True)
    check("refusal reattached xpad", dev.kernel and ("attach", 0) in events, events)

    dev, events, sysfs, link = x_pad()
    try:
        with vader4.XInputController(sysfs, link=link):
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        pass
    check("Ctrl+C reattached xpad", dev.kernel and events[-2] == ("attach", 0), events)

    dev, events, sysfs, link = x_pad(fail_claim=True)
    try:
        vader4.XInputController(sysfs, link=link)
        check("claim failure raised", False)
    except OSError:
        check("claim failure raised", True)
    check("claim failure reattached xpad", dev.kernel and ("attach", 0) in events, events)

    dev, events, sysfs, link = x_pad(respond=lambda p: [])
    try:
        vader4.XInputController(sysfs, link=link, wait=0.05)
        check("silent pad raises", False)
    except vader4.ProtocolError:
        check("silent pad raises", True)
    check("silent pad reattached xpad", dev.kernel)

    dev, events, sysfs, link = x_pad(fail_attach=True)
    ctrl = vader4.XInputController(sysfs, link=link)
    try:
        ctrl.close()
        check("failed reattach is reported", False)
    except OSError as exc:
        check("failed reattach is reported", "reattach" in str(exc))
    check("release still happened", ("release", 0) in events)


def test_xinput_real_capture():
    """Packets captured from the real pad (XInput over its dongle)."""
    info = vader4.parse_xinput(bytes.fromhex(
        "0014000000000000000000000000a51055f6ad33383369040200010000004000"))
    check("captured info parses", info is not None and info.kind == "info")
    i = info.info
    check("captured: type 85, fw 6.9.3.3, battery 100%, wch wireless",
          (i.device_type, i.firmware, i.battery_percent, i.cpu, i.connection)
          == (85, "6.9.3.3", 100, "wch", "wireless"),
          (i.device_type, i.firmware, i.battery_percent, i.cpu, i.connection))
    dongle = vader4.parse_xinput(bytes.fromhex(
        "0014000000000000000000000000a51104560000000000000000000010000000"))
    check("captured dongle reply", dongle.kind == "dongle" and dongle.data == b"\x04\x56")
    check("plain input report is not a reply",
          vader4.parse_xinput(bytes([0, 0x14]) + bytes(30)) is None)


def test_xinput_accept_types():
    dev, events, sysfs, link = x_pad(device_type=85)
    with vader4.XInputController(sysfs, link=link) as ctrl:
        check("type 85 accepted by default", ctrl.info.device_type == 85
              and ctrl.info.is_vader4_pro)
    check("accept session reattached", dev.kernel)
    dev, events, sysfs, link = x_pad(device_type=81)
    with vader4.XInputController(sysfs, link=link, accept=(81,)) as ctrl:
        check("other type accepted only when asked", ctrl.info.device_type == 81)


def test_xinput_repeat_is_separated():
    """The dongle ignores a frame identical to the previous one."""
    last = [None]

    def respond(packet):
        if packet == last[0]:
            return []
        last[0] = packet
        if packet[1] == 16:
            return [x_info()]
        if packet[1] == 17:
            return [x_reply(17, b"\x04\x56")]
        return []
    dev, events, sysfs, link = x_pad(respond=respond)
    with vader4.XInputController(sysfs, link=link) as ctrl:
        info = vader4.read_info(ctrl, retries=1)
        check("repeat info answered", info.device_type == 105)
        check("a dongle-version separator went between",
              dev.written[-2] == vader4.xinput_command(17)
              and dev.written[-1] == vader4.xinput_command(16), [w[1] for w in dev.written])
    check("separated session reattached", dev.kernel)


def _ack_pad(ack_for):
    """XInput fake whose data-parcel ack number is ack_for(k)."""
    def respond(packet):
        if packet[1] == 16:
            return [x_info()]
        if packet[1] == 42:
            return [x_reply(42)]
        if packet[1] == 41:
            got = ack_for(packet[13])
            return [] if got is None else [x_reply(*got)] if isinstance(got, tuple) \
                else [x_reply(41, bytes([got]))]
        return []
    return x_pad(respond=respond)


def test_ack_conventions():
    for label, rule in (("acks numbered k", lambda k: k),
                        ("acks numbered k+1", lambda k: k + 1)):
        dev, events, sysfs, link = _ack_pad(rule)
        acks = []
        with vader4.XInputController(sysfs, link=link) as ctrl:
            sent = vader4.write_lighting(ctrl, vader4.Lighting(bytes(500)), wait=0.3, acks=acks)
        check(f"{label}: accepted", sent == 51 and len(dev.written) == 52, len(dev.written))
        check(f"{label}: recorded", len(acks) == 51)
    for label, rule in (("stale ack (k-1)", lambda k: k - 1 if k else 0),
                        ("wrong command ack", lambda k: (36, bytes([k + 1])))):
        dev, events, sysfs, link = _ack_pad(rule)
        try:
            with vader4.XInputController(sysfs, link=link) as ctrl:
                vader4.write_lighting(ctrl, vader4.Lighting(bytes(500)), wait=0.05, retries=2)
            check(f"{label}: refused", False)
        except vader4.ProtocolError:
            check(f"{label}: refused", True)
        # parcel 0 (its ack 0 is valid), then parcel 1 twice -- separators aside
        packs = [w for w in dev.written if w[1] == 41]
        check(f"{label}: stopped early", len(packs) <= 3, len(packs))
        check(f"{label}: xpad back", dev.kernel)
    dev, events, sysfs, link = _ack_pad(lambda k: k)
    with vader4.XInputController(sysfs, link=link) as ctrl:
        try:
            vader4.write_lighting(ctrl, vader4.Lighting(bytes(490)))
            check("short blob refused before sending", False)
        except vader4.ProtocolError:
            check("short blob refused before sending", len(dev.written) == 1)


def test_backup_files():
    import importlib.machinery
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "flydigi-vader4")
    loader = importlib.machinery.SourceFileLoader("flydigi_vader4_tool", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    tool = importlib.util.module_from_spec(spec)
    loader.exec_module(tool)
    tool.BACKUP_DIR = tempfile.mkdtemp(prefix="vader4-backups-")
    TEMP_DIRS.append(tool.BACKUP_DIR)
    blob = bytearray(500)
    blob[0:2], blob[4], blob[5], blob[6], blob[7], blob[8] = b"\x00\x02", 5, 50, 100, 5, 1
    blob[20:23] = b"\x32\x32\x00"
    info = vader4.parse_xinput(x_info(85)).info
    first = tool.save_backup(bytes(blob), info)
    second = tool.save_backup(bytes(blob), info)
    check("backup names are unique", first != second and os.path.exists(second))
    check("backup is the raw bytes", open(first, "rb").read() == bytes(blob))
    import json
    side = json.load(open(first[:-4] + ".json"))
    check("sidecar decodes", side["lighting"]["mode_name"] == "streamlined"
          and side["lighting"]["leds"][0][0] == "#323200" and side["device_type"] == 85)
    check("load_backup round-trips", tool.load_backup(first) == bytes(blob))
    with open(first, "r+b") as fh:
        fh.write(b"\x01")
    try:
        tool.load_backup(first)
        check("tampered backup refused", False)
    except vader4.ProtocolError:
        check("tampered backup refused", True)
    short = os.path.join(tool.BACKUP_DIR, "short.bin")
    open(short, "wb").write(bytes(490))
    try:
        tool.load_backup(short)
        check("490-byte backup refused", False)
    except vader4.ProtocolError:
        check("490-byte backup refused", True)


def test_xinput_refuses_other_products():
    dev, events, sysfs, link = x_pad(product="Controller")
    try:
        vader4.XInputController(sysfs, link=link)
        check("non-VADER4 product refused", False)
    except device.DeviceNotFound:
        check("non-VADER4 product refused", True)
    check("nothing detached for a stranger", events == [] and dev.written == [], events)


def test_xinput_write_parcels_over_link():
    def respond(packet):
        if packet[1] == 16:
            return [x_info()]
        if packet[1] == 42:
            return [x_reply(42)]
        if packet[1] == 41:
            return [x_reply(41, bytes([packet[13] + 1]))]
        return []
    dev, events, sysfs, link = x_pad(respond=respond)
    with vader4.XInputController(sysfs, link=link) as ctrl:
        light = vader4.Lighting(bytes(500))
        light.set_solid((1, 2, 3), 50)
        sent = vader4.write_lighting(ctrl, light, wait=0.3)
    check("xinput write sent start + 50", sent == 51)
    check("xinput write bytes", dev.written[1:] == vader4.write_parcels(light.blob, 0, "xinput"))
    check("reattached after write", dev.kernel)


def test_xinput_discovery_and_dispatch():
    vader = fake_sysfs()
    xbox = fake_sysfs(product="Controller")
    saved = (vader4.glob.glob,)
    try:
        def fake_glob(pattern):
            if pattern.startswith("/sys/bus/usb/devices"):
                return [xbox, vader]
            return []
        vader4.glob.glob = fake_glob
        every = list(vader4.find_xinput_devices())
        only = list(vader4.find_xinput_devices(vader4_only=True))
        check("both 028e devices seen", sorted(every) == sorted([vader, xbox]))
        check("only the VADER4 kept", only == [vader], only)
        check("find_device falls back to xinput", vader4.find_device() == "usb:" + vader)
        vader4.glob.glob = lambda pattern: [xbox] if pattern.startswith("/sys") else []
        try:
            vader4.find_device()
            check("lone Xbox pad refused", False)
        except device.DeviceNotFound as exc:
            check("lone Xbox pad refused", "VADER4" in str(exc))
    finally:
        vader4.glob.glob, = saved
    dev, events, sysfs, link = x_pad()
    ctrl = vader4.Controller("usb:" + sysfs, link=link)
    check("Controller dispatches usb: paths", isinstance(ctrl, vader4.XInputController)
          and ctrl.mode == "xinput")
    ctrl.close()
    check("dispatch session reattached", dev.kernel)


def main():
    for test in (test_frames, test_write_parcels, test_parse_dinput,
                 test_parse_xinput, test_assembler, test_lighting_codec,
                 test_discovery, test_transport_reads,
                 test_require_refuses_other_pads, test_write_lighting,
                 test_write_stops_without_ack,
                 test_xinput_session_detaches_and_reattaches,
                 test_xinput_reattaches_on_errors,
                 test_xinput_real_capture,
                 test_xinput_accept_types,
                 test_ack_conventions,
                 test_xinput_repeat_is_separated,
                 test_backup_files,
                 test_xinput_refuses_other_products,
                 test_xinput_write_parcels_over_link,
                 test_xinput_discovery_and_dispatch):
        test()
    for root in TEMP_DIRS:
        shutil.rmtree(root, ignore_errors=True)
    for name in FAILED:
        print(f"  FAILED: {name}")
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
