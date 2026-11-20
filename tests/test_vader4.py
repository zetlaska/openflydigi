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
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flydigi import device, vader4  # noqa: E402

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
    check("490-byte lighting is 49 parcels + start",
          len(vader4.write_parcels(bytes(490), 0)) == 50)
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
    blob = bytearray(490)
    blob[6], blob[7], blob[8] = 80, 5, vader4.MODE_STEADY
    off = vader4.OFF_FRAMES + (2 * 10 + 0) * 3      # LED 2, frame 0
    blob[off:off + 3] = b"\x01\x02\x03"
    light = vader4.Lighting(blob)
    check("LED-major offsets", light.led(2, 0) == (1, 2, 3))
    check("15 whole LED slots in 490 bytes", light.slots == 15, light.slots)
    light.set_solid((0, 0xE5, 0xFF), brightness=150)
    check("solid keeps length", len(light.blob) == 490)
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
        light.led(15, 0)
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
    stored = bytearray(490)
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
    pad = ScriptedPad(lambda p: [info_report(device_type=85)])
    try:
        ctrl = pad.controller()
        try:
            vader4.require(ctrl)
            check("Vader 4 (85) refused", False)
        except vader4.ProtocolError as exc:
            check("Vader 4 (85) refused", "Vader 4" in str(exc))
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
        light = vader4.Lighting(bytes(490))
        light.set_solid((255, 0, 0), 80)
        sent = vader4.write_lighting(ctrl, light, wait=0.5)
        check("write sent start + 49 parcels", sent == 50 and len(pad.written) == 50,
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
            vader4.write_lighting(ctrl, vader4.Lighting(bytes(490)), wait=0.05, retries=2)
            check("silent pad aborts the write", False)
        except vader4.ProtocolError:
            check("silent pad aborts the write", True)
        check("stopped at the start packet, retried once", len(pad.written) == 2,
              len(pad.written))
        ctrl.close()
    finally:
        pad.close()


def main():
    for test in (test_frames, test_write_parcels, test_parse_dinput,
                 test_parse_xinput, test_assembler, test_lighting_codec,
                 test_discovery, test_transport_reads,
                 test_require_refuses_other_pads, test_write_lighting,
                 test_write_stops_without_ack):
        test()
    for name in FAILED:
        print(f"  FAILED: {name}")
    print(f"\n{len(PASSED)}/{len(PASSED) + len(FAILED)} passed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
