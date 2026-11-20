# SPDX-FileCopyrightText: 2026 zetlaska
#
# SPDX-License-Identifier: MIT

"""Flydigi Vader 4 Pro (device type 105) over Flydigi's *old* protocol.

**Not hardware-validated.** Nothing here has been sent to a Vader 4 Pro yet.
The facts below -- ids, command numbers, offsets -- were read out of
flydigictl's `vader4-pro-support` branch (github.com/Razuer/flydigictl, itself
a reading of Space Station). That project is GPL-3.0; this file is an
independent implementation of the same protocol facts, not a translation of
its code, which is why it can stay MIT like the rest of `flydigi/`.

Space Station picks the protocol by vendor id -- `IsOldProtocol()` is
`VendorId != 0x37D7` -- and a Vader 4 Pro never carries 37d7, so none of
`device.py`'s 5A A5 framing applies. It has two wired/2.4G personalities:

  DInput   04b4:2412, HID, the vendor interface is interface 2 (hidraw).
           Out: 12 bytes, `05 cmd args...`, no checksum.
           In:  `p[15]` names the reply (236 info, 235 config, 229 lighting,
                234/231/51 write acks); `p[3]` is the device type, packet
                index or ack number. Dongle version is `04 11 lo hi`.
  XInput   045e:028e, *not* HID: xpad owns it and the vendor channel is the
           interrupt pipe, out endpoint 5 / in endpoint 1 on interface 0, so it
           needs usbfs and detaching xpad (no input while configuring).
           Out: 15 bytes, `A5 cmd args...`, byte 14 = 8-bit sum of 0..13.
           In:  `p[14] == A5`, `p[15]` the reply id, payload from `p[16]`.

Only DInput has a transport here, because it is the one that fits this
project's hidraw layer. XInput frames are built and parsed so a usbfs transport
can be added without revisiting the protocol. Bluetooth / Switch modes speak
neither.

Blobs (profile, lighting) move in 10-byte parcels: a start packet stating the
parcel count, then `cmd, 10 data bytes, 0xA0, index`, each acked. A read
streams parcels back in order -- 52 for a profile, 49 (490 bytes) for lighting.

**Lighting layout is LED-major**, unlike the Apex 5's frame-major blob in
`lighting.py`: header bytes 0..19 sit at the same offsets (version, type,
loop start/end, cycle time, brightness, LED count, mode), then for each LED
ten frames of RGB. flydigictl's steady colour -- frame 0 of LEDs 0..4 set,
`loop_end = 0`, LED count 5 -- only makes sense read that way. This is the
first thing to confirm on hardware.
"""
import collections
import dataclasses
import glob
import os

from . import device

DINPUT_VID, DINPUT_PID = 0x04B4, 0x2412
DINPUT_INTERFACE = 2
XINPUT_VID, XINPUT_PID = 0x045E, 0x028E

DEVICE_TYPE = 105
# Other pads that answer the same old protocol, so a refusal can say what it
# found. flydigictl's tables; only 105 is what this module is for.
KNOWN_TYPES = {19: "Apex 2", 80: "Vader 3 Pro", 81: "Vader 3 Pro ONE PIECE",
               85: "Vader 4", 105: "Vader 4 Pro"}

PARCEL_DATA = 10
SEQ_MARK = 0xA0
CONFIG_PARCELS = 52
LED_PARCELS = 49

# -- DInput ------------------------------------------------------------------
D_REPORT = 0x05
D_COMMAND_LEN = 12
D_CMD_DONGLE = 17
D_CMD_INFO = 236
D_CMD_READ_CONFIG = 235
D_CMD_READ_LED = 229
D_CMD_CONFIG_START = 234
D_CMD_CONFIG_PACK = 34
D_CMD_LED_START = 231
D_CMD_LED_PACK = 51
D_REPLY_ACKS = (D_CMD_CONFIG_START, D_CMD_LED_START, D_CMD_LED_PACK)

# -- XInput ------------------------------------------------------------------
X_MAGIC = 0xA5
X_FRAME_LEN = 15
X_CMD_INFO = 16
X_CMD_DONGLE = 17
X_REPLY_CONFIG_ID = 32
X_CMD_READ_CONFIG = 33
X_REPLY_CONFIG = 34
X_CMD_CONFIG_PACK = 36
X_CMD_CONFIG_START = 37
X_CMD_READ_LED = 38
X_REPLY_LED = 39
X_CMD_LED_PACK = 41
X_CMD_LED_START = 42

# -- lighting blob -----------------------------------------------------------
OFF_TYPE = 2
OFF_LOOP_START = 3
OFF_LOOP_END = 4
OFF_LOOP_TIME = 5
OFF_BRIGHTNESS = 6
OFF_LED_COUNT = 7
OFF_MODE = 8
OFF_FRAMES = 20
FRAMES_PER_LED = 10
LED_SLOTS = 16
LED_COUNT = 5          # what flydigictl's generators drive on this pad
BRIGHTNESS_MAX = 100

# Old-protocol mode numbers (not `lighting.EFFECT_*`, which are Apex 5 ids).
MODE_OFF = 0
MODE_STREAMLINED = 1
MODE_BREATHING = 2
MODE_GRADIENT = 3
MODE_FEEDBACK = 4
MODE_STEADY = 5
MODE_NAMES = {MODE_OFF: "off", MODE_STREAMLINED: "streamlined",
              MODE_BREATHING: "breathing", MODE_GRADIENT: "gradient",
              MODE_FEEDBACK: "feedback", MODE_STEADY: "steady"}


class ProtocolError(Exception):
    pass


# ---------------------------------------------------------------------------
# frames

def dinput_command(cmd, *args):
    buf = bytearray(D_COMMAND_LEN)
    buf[0], buf[1] = D_REPORT, cmd
    buf[2:2 + len(args)] = bytes(args)
    return bytes(buf)


def _xsum(buf):
    out = bytearray(buf)
    out[-1] = sum(out[:-1]) & 0xFF
    return bytes(out)


def xinput_command(cmd, *args):
    buf = bytearray(X_FRAME_LEN)
    buf[0], buf[1] = X_MAGIC, cmd
    buf[2:2 + len(args)] = bytes(args)
    return _xsum(buf)


def _chunks(blob):
    blob = bytes(blob)
    count = max(1, -(-len(blob) // PARCEL_DATA))
    for i in range(count):
        part = blob[i * PARCEL_DATA:(i + 1) * PARCEL_DATA]
        yield i, part + bytes(PARCEL_DATA - len(part))


def write_parcels(blob, cfg_id, mode="dinput", led=True):
    """Every packet of a blob write, start packet first. Builds; sends nothing."""
    count = max(1, -(-len(blob) // PARCEL_DATA))
    if count > 255:
        raise ProtocolError(f"blob of {len(blob)} bytes is too long")
    if mode == "dinput":
        head = bytearray(PARCEL_DATA + 4)
        start = ((D_REPORT, D_CMD_LED_START, cfg_id, count) if led else
                 (D_REPORT, D_CMD_CONFIG_START, count, SEQ_MARK, cfg_id))
        head[:len(start)] = bytes(start)
        inner = D_CMD_LED_PACK if led else D_CMD_CONFIG_PACK
        packets = [bytes(head)]
        for i, part in _chunks(blob):
            packets.append(bytes((D_REPORT, inner)) + part + bytes((SEQ_MARK, i)))
        return packets
    if mode == "xinput":
        head = bytearray(X_FRAME_LEN)
        start = ((X_MAGIC, X_CMD_LED_START, cfg_id, count) if led else
                 (X_MAGIC, X_CMD_CONFIG_START, count, SEQ_MARK, cfg_id))
        head[:len(start)] = bytes(start)
        inner = X_CMD_LED_PACK if led else X_CMD_CONFIG_PACK
        packets = [_xsum(head)]
        for i, part in _chunks(blob):
            packets.append(_xsum(bytes((X_MAGIC, inner)) + part
                                 + bytes((SEQ_MARK, i, 0))))
        return packets
    raise ValueError(f"unknown mode {mode!r}")


# ---------------------------------------------------------------------------
# replies

@dataclasses.dataclass
class Info:
    device_type: int
    mac: bytes
    fw_lo: int
    fw_hi: int
    battery_raw: int
    cpu_type: int
    connection_type: int
    motion_sensor: int
    mode: str = "dinput"

    @property
    def firmware(self):
        return (f"{self.fw_hi >> 4}.{self.fw_hi & 15}."
                f"{self.fw_lo >> 4}.{self.fw_lo & 15}")

    @property
    def name(self):
        return KNOWN_TYPES.get(self.device_type, f"unknown type {self.device_type}")

    @property
    def is_vader4_pro(self):
        return self.device_type == DEVICE_TYPE

    @property
    def battery_percent(self):
        """0..4 is a charge level on the Vader 4 / 4 Pro, 5..100 a percentage."""
        raw = self.battery_raw
        if self.device_type in (85, 105):
            if raw <= 4:
                return raw * 25
            if raw <= 100:
                return raw
        return None

    @property
    def cpu(self):
        wch = self.cpu_type > 0 or (self.fw_hi >> 4 >= 6 and self.fw_hi & 15 >= 1)
        return "wch" if wch else "nordic"

    @property
    def connection(self):
        if self.cpu != "wch":
            return "unknown"
        return "wired" if self.connection_type == 1 else "wireless"

    @property
    def mac_text(self):
        return ":".join(f"{b:02x}" for b in self.mac)


Reply = collections.namedtuple("Reply", "kind index data info",
                               defaults=(None, b"", None))


def parse_dinput(p):
    """One DInput report -> Reply, or None if it is not a command reply."""
    p = bytes(p)
    if len(p) >= 16:
        if p[15] == D_CMD_READ_CONFIG:
            return Reply("config", p[3], p[5:15])
        if p[15] == D_CMD_READ_LED:
            return Reply("led", p[3], p[5:15])
    if len(p) >= 4 and p[0] == 0x04 and p[1] == D_CMD_DONGLE:
        return Reply("dongle", data=p[2:4])
    if len(p) >= 16:
        if p[15] in D_REPLY_ACKS:
            return Reply("ack", p[3], bytes((p[15],)))
        if p[15] == D_CMD_INFO:
            return Reply("info", info=Info(
                p[3], p[5:9], p[9], p[10], p[11], p[12], p[13], p[14], "dinput"))
    return None


def parse_xinput(p):
    """One XInput interrupt packet -> Reply, or None."""
    p = bytes(p)
    if len(p) < 17 or p[14] != X_MAGIC:
        return None
    cmd = p[15]
    if cmd == X_CMD_INFO and len(p) >= 27:
        return Reply("info", info=Info(
            p[16], p[17:21], p[21], p[22], p[23], p[24], p[25], p[26], "xinput"))
    if cmd == X_CMD_DONGLE and len(p) >= 18:
        return Reply("dongle", data=p[16:18])
    if cmd == X_REPLY_CONFIG_ID:
        return Reply("config_id", p[16])
    if cmd in (X_REPLY_CONFIG, X_REPLY_LED) and len(p) >= 27:
        return Reply("config" if cmd == X_REPLY_CONFIG else "led", p[16], p[17:27])
    if cmd in (X_CMD_CONFIG_START, 35, X_CMD_LED_START):
        return Reply("ack", 0, bytes((cmd,)))
    if cmd in (X_CMD_CONFIG_PACK, X_CMD_LED_PACK):
        return Reply("ack", p[16], bytes((cmd,)))
    return None


class Assembler:
    """Reassemble a parcel stream. Parcels must arrive in order, from 0;
    anything else is ignored rather than guessed at."""

    def __init__(self, parcels):
        self.parcels = parcels
        self.received = 0
        self.buf = bytearray(parcels * PARCEL_DATA)

    def add(self, index, data):
        if index != self.received or index >= self.parcels:
            return False
        self.buf[index * PARCEL_DATA:(index + 1) * PARCEL_DATA] = bytes(data[:PARCEL_DATA]).ljust(PARCEL_DATA, b"\0")
        self.received += 1
        return True

    @property
    def done(self):
        return self.received == self.parcels

    @property
    def blob(self):
        return bytes(self.buf) if self.done else None


# ---------------------------------------------------------------------------
# lighting

class Lighting:
    """The pad's stored lighting blob, LED-major. Edits keep its length."""

    def __init__(self, blob):
        if len(blob) < OFF_FRAMES:
            raise ProtocolError(f"lighting blob of {len(blob)} bytes is too short")
        self.blob = bytearray(blob)

    @property
    def mode(self):
        return self.blob[OFF_MODE]

    @property
    def mode_name(self):
        return MODE_NAMES.get(self.mode, f"mode {self.mode}")

    @property
    def brightness(self):
        return self.blob[OFF_BRIGHTNESS]

    @brightness.setter
    def brightness(self, value):
        self.blob[OFF_BRIGHTNESS] = max(0, min(BRIGHTNESS_MAX, int(value)))

    @property
    def led_count(self):
        return self.blob[OFF_LED_COUNT]

    @property
    def cycle_time(self):
        return self.blob[OFF_LOOP_TIME]

    @property
    def loop(self):
        return self.blob[OFF_LOOP_START], self.blob[OFF_LOOP_END]

    @property
    def slots(self):
        """How many LEDs' worth of frames the blob actually holds, whole."""
        return min(LED_SLOTS, (len(self.blob) - OFF_FRAMES) // (3 * FRAMES_PER_LED))

    def _offset(self, led, frame):
        if not 0 <= led < self.slots:
            raise IndexError(f"led {led} out of range (have {self.slots})")
        if not 0 <= frame < FRAMES_PER_LED:
            raise IndexError(f"frame {frame} out of range")
        return OFF_FRAMES + (led * FRAMES_PER_LED + frame) * 3

    def led(self, led, frame=0):
        o = self._offset(led, frame)
        return tuple(self.blob[o:o + 3])

    def set_led(self, led, frame, colour):
        o = self._offset(led, frame)
        self.blob[o:o + 3] = bytes(max(0, min(255, int(c))) for c in colour[:3])

    def _clear(self):
        self.blob[OFF_FRAMES:] = bytes(len(self.blob) - OFF_FRAMES)

    def set_off(self):
        self._clear()
        self.blob[OFF_TYPE] = 0
        self.blob[OFF_LOOP_START] = self.blob[OFF_LOOP_END] = 0
        self.blob[OFF_LED_COUNT] = 0
        self.blob[OFF_MODE] = MODE_OFF

    def set_solid(self, colour, brightness=None):
        self._clear()
        for led in range(min(LED_COUNT, self.slots)):
            self.set_led(led, 0, colour)
        self.blob[OFF_TYPE] = 0
        self.blob[OFF_LOOP_START] = self.blob[OFF_LOOP_END] = 0
        self.blob[OFF_LED_COUNT] = LED_COUNT
        self.blob[OFF_MODE] = MODE_STEADY
        if brightness is not None:
            self.brightness = brightness

    def describe(self):
        text = f"{self.mode_name}, brightness {self.brightness}/{BRIGHTNESS_MAX}"
        if self.mode in (MODE_STEADY, MODE_BREATHING) and self.slots:
            text += " #%02X%02X%02X" % self.led(0, 0)
        return text


# ---------------------------------------------------------------------------
# discovery and transport (DInput hidraw only)

def interface_number(node):
    """bInterfaceNumber of the USB interface a hidraw node hangs off, or None."""
    try:
        hid = os.path.realpath(f"/sys/class/hidraw/{node}/device")
        with open(os.path.join(os.path.dirname(hid), "bInterfaceNumber")) as fh:
            return int(fh.read().strip(), 16)
    except (OSError, ValueError):
        return None


def find_dinput_nodes():
    for path in sorted(glob.glob("/dev/hidraw*"),
                       key=lambda p: int(p[len("/dev/hidraw"):] or 0)):
        node = os.path.basename(path)
        if (device.hid_ids(node) == (DINPUT_VID, DINPUT_PID)
                and interface_number(node) == DINPUT_INTERFACE):
            yield path


def find_xinput_devices():
    """USB devices enumerating as 045e:028e -- possibly a Vader 4 Pro in XInput
    mode, possibly a real Xbox 360 pad. Listed only; nothing opens them."""
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            with open(os.path.join(dev, "idVendor")) as fh:
                vid = int(fh.read(), 16)
            with open(os.path.join(dev, "idProduct")) as fh:
                pid = int(fh.read(), 16)
        except (OSError, ValueError):
            continue
        if (vid, pid) == (XINPUT_VID, XINPUT_PID):
            yield dev


def find_device():
    for path in find_dinput_nodes():
        return path
    if any(True for _ in find_xinput_devices()):
        raise device.DeviceNotFound(
            "an XInput-mode pad (045e:028e) is attached, but only DInput mode "
            "is driven so far -- switch the Vader 4 Pro to DInput")
    raise device.DeviceNotFound(
        "no Vader 4 Pro in DInput mode (04b4:2412) found -- wake the pad or "
        "check the dongle/cable")


class Controller(device.Controller):
    """hidraw handle on the DInput vendor interface. Reuses the base class's
    claim, drain and send; only discovery differs."""

    def __new__(cls, path=None):
        return super().__new__(cls, path or find_device())


def _replies(ctrl, packet, wait, until):
    out = []

    def check(raw):
        reply = parse_dinput(raw[-1])
        if reply is not None:
            out.append(reply)
        return until(out)
    ctrl.send(packet, wait=wait, until=check)
    return out


def read_info(ctrl, wait=0.6, retries=3):
    for _ in range(retries):
        got = _replies(ctrl, dinput_command(D_CMD_INFO), wait,
                       lambda rs: any(r.kind == "info" for r in rs))
        for r in got:
            if r.kind == "info":
                return r.info
    raise ProtocolError("no device-info reply")


def require(ctrl, wait=0.6):
    info = read_info(ctrl, wait=wait)
    if not info.is_vader4_pro:
        raise ProtocolError(f"this is a {info.name}, not a Vader 4 Pro; refusing")
    return info


def read_lighting(ctrl, cfg_id=0, wait=2.0, retries=3):
    for _ in range(retries):
        asm = Assembler(LED_PARCELS)

        def until(rs):
            r = rs[-1] if rs else None
            if r is not None and r.kind == "led":
                asm.add(r.index, r.data)
            return asm.done
        _replies(ctrl, dinput_command(D_CMD_READ_LED, cfg_id), wait, until)
        if asm.done:
            return Lighting(asm.blob)
    raise ProtocolError("lighting read did not complete")


def write_lighting(ctrl, lighting, cfg_id=0, wait=3.0, retries=3):
    """Stream a lighting blob back, one acked packet at a time. UNTESTED on
    hardware. A packet counts as delivered when the pad acks with a number at
    least its position in the sequence (start packet = 0), which is the rule
    flydigictl's writer uses."""
    packets = write_parcels(lighting.blob, cfg_id, "dinput", led=True)
    with ctrl.claim():
        for position, packet in enumerate(packets):
            for _ in range(retries):
                got = _replies(ctrl, packet, wait, lambda rs: any(
                    r.kind == "ack" and r.index >= position for r in rs))
                if any(r.kind == "ack" and r.index >= position for r in got):
                    break
            else:
                raise ProtocolError(f"no ack for packet {position} of {len(packets)}")
    return len(packets)
