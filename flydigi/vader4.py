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

Both have a transport: DInput through hidraw like every other pad here, XInput
through pyusb (python-pyusb, libusb) with xpad detached from interface 0 for
the length of a session -- see `XInputController`. Bluetooth / Switch modes
speak neither.

Blobs (profile, lighting) move in 10-byte parcels: a start packet stating the
parcel count, then `cmd, 10 data bytes, 0xA0, index`, each acked. A read
streams parcels back in order -- 52 for a profile, 49 (490 bytes) for lighting.

**Lighting layout is LED-major** (confirmed: the stock streamlined blob read
back from the pad is exactly flydigictl's per-LED rainbow table), unlike the Apex 5's frame-major blob in
`lighting.py`: header bytes 0..19 sit at the same offsets (version, type,
loop start/end, cycle time, brightness, LED count, mode), then for each LED
ten frames of RGB. flydigictl's steady colour -- frame 0 of LEDs 0..4 set,
`loop_end = 0`, LED count 5 -- only makes sense read that way. This is the
first thing to confirm on hardware.
"""
import atexit
import collections
import contextlib
import dataclasses
import glob
import os
import threading
import time

from . import device

DINPUT_VID, DINPUT_PID = 0x04B4, 0x2412
DINPUT_INTERFACE = 2
XINPUT_VID, XINPUT_PID = 0x045E, 0x028E

DEVICE_TYPE = 105
# What a session accepts by default. flydigictl lists the Vader 4 Pro as 105;
# the one measured here -- product "Flydigi VADER4", firmware 6.9.3.3 -- reports
# 85, which flydigictl calls "Vader 4". Both are the same old protocol and the
# same 500-byte lighting blob (measured on the 85). Anything else is refused.
ACCEPTED_TYPES = (105, 85)
# Other pads that answer the same old protocol, so a refusal can say what it
# found. flydigictl's tables; only 105 is what this module is for.
KNOWN_TYPES = {19: "Apex 2", 80: "Vader 3 Pro", 81: "Vader 3 Pro ONE PIECE",
               85: "Vader 4 Pro (type 85)", 105: "Vader 4 Pro"}

PARCEL_DATA = 10
SEQ_MARK = 0xA0
CONFIG_PARCELS = 52
# **The pad streams 50 lighting parcels, 500 bytes**, measured: indices 0..49
# in reply to a read. flydigictl's reader stops at 49 (490 bytes) and drops the
# last, while its writer sends 500 for off/streamlined -- so 490 was its
# reader's limit, not the pad's size. 20 header bytes + 16 LEDs x 10 frames x 3.
LED_PARCELS = 50
LED_BLOB_LEN = LED_PARCELS * 10

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
        return self.device_type in ACCEPTED_TYPES

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

    def fields(self):
        """The decoded header and the first frame of each LED, for a sidecar."""
        return {
            "length": len(self.blob),
            "version": self.blob[0:2].hex(),
            "type": self.blob[OFF_TYPE],
            "loop_start": self.blob[OFF_LOOP_START],
            "loop_end": self.blob[OFF_LOOP_END],
            "cycle_time": self.cycle_time,
            "brightness": self.brightness,
            "led_count": self.led_count,
            "mode": self.mode,
            "mode_name": self.mode_name,
            "reserved": self.blob[9:OFF_FRAMES].hex(),
            "leds": [["#%02X%02X%02X" % self.led(led, f) for f in range(FRAMES_PER_LED)]
                     for led in range(self.slots)],
        }

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


def _read_attr(dev, name):
    try:
        with open(os.path.join(dev, name)) as fh:
            return fh.read().strip()
    except OSError:
        return None


def find_xinput_devices(vader4_only=False):
    """USB devices enumerating as 045e:028e, as sysfs paths. Nothing is opened.

    045e:028e is the generic Xbox 360 id, so a real Microsoft pad or another
    clone carries it too. `vader4_only` keeps the ones whose product string
    names a VADER4 -- the first of two gates; the second is device type 105 in
    the info reply, which `XInputController` checks before anything else.
    """
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        try:
            vid = int(_read_attr(dev, "idVendor") or "", 16)
            pid = int(_read_attr(dev, "idProduct") or "", 16)
        except ValueError:
            continue
        if (vid, pid) != (XINPUT_VID, XINPUT_PID):
            continue
        if vader4_only and XINPUT_PRODUCT_TAG not in (_read_attr(dev, "product") or "").upper():
            continue
        yield dev


def find_device():
    """First Vader 4 Pro, DInput preferred: a hidraw path, or `usb:<sysfs>`."""
    for path in find_dinput_nodes():
        return path
    for dev in find_xinput_devices(vader4_only=True):
        return XINPUT_PREFIX + dev
    if any(True for _ in find_xinput_devices()):
        raise device.DeviceNotFound(
            "an Xbox 360-style pad (045e:028e) is attached, but its product "
            "string does not name a VADER4; refusing to touch it")
    raise device.DeviceNotFound(
        "no Vader 4 Pro found (DInput 04b4:2412 or XInput 045e:028e VADER4) "
        "-- wake the pad or check the dongle/cable")


class Controller(device.Controller):
    """A Vader 4 Pro session, in whichever mode the pad is in.

    DInput: hidraw handle on the vendor interface, reusing the base class's
    claim, drain and send. XInput: `Controller("usb:/sys/bus/usb/devices/X")`
    -- or a bare `Controller()` when only an XInput pad is attached -- hands
    back an `XInputController` instead, with the same send/claim/close.
    """
    mode = "dinput"

    def __new__(cls, path=None, **kwargs):
        path = path or find_device()
        if path.startswith(XINPUT_PREFIX):
            return XInputController(path[len(XINPUT_PREFIX):], **kwargs)
        return super().__new__(cls, path)

    def __init__(self, path=None, **kwargs):
        super().__init__(path)


# -- XInput transport ---------------------------------------------------------
#
# The vendor channel shares interface 0's interrupt pipes with the gamepad
# stream, and xpad owns that interface, so a session detaches xpad from
# interface 0 *only* (the device stays configured, nothing is reset), claims
# it, talks, then releases and reattaches. The pad gives no game input for that
# long. Reattaching is in `close`, which `__exit__`, every failure path in
# `__init__` and `atexit` all reach, so an exception or Ctrl+C still hands
# the pad back to xpad. SIGKILL cannot be caught; replugging recovers then.

XINPUT_PREFIX = "usb:"
XINPUT_PRODUCT_TAG = "VADER4"
XINPUT_INTERFACE = 0
XINPUT_EP_OUT = 0x05
XINPUT_EP_IN = 0x81
XINPUT_READ_SIZE = 64
XINPUT_POLL_MS = 20


# **Replies ride inside input reports.** Measured on a Vader 4 Pro over its
# dongle (XInput, firmware 6.9.3.3): every packet on endpoint 0x81 is 32 bytes
# and starts `00 14` like an Xbox 360 input report, and a vendor reply is one
# of those with `A5 <reply id>` at bytes 14..15 and its payload from 16 --
#   00 14 00..00 a5 10 55 f6 ad 33 38 33 69 04 02 00 01 ...   (info)
# so nothing can be filtered on the header; `parse_xinput` decides.

class PyUsbLink:
    """Interface 0 of one USB device, through pyusb (python-pyusb + libusb).

    `usb_core` / `usb_util` are injectable so the detach/reattach logic can be
    tested without a device; normally they are pyusb's own modules.
    """

    def __init__(self, sysfs, usb_core=None, usb_util=None):
        if usb_core is None or usb_util is None:
            try:
                import usb.core
                import usb.util
            except ImportError as exc:
                raise device.DeviceNotFound(
                    "XInput mode needs pyusb (python-pyusb) and libusb") from exc
            usb_core, usb_util = usb.core, usb.util
        self.core, self.util = usb_core, usb_util
        self.sysfs = sysfs
        self.dev = None
        self.detached = False
        self.claimed = False
        bus, addr = _read_attr(sysfs, "busnum"), _read_attr(sysfs, "devnum")
        if bus is None or addr is None:
            raise device.DeviceNotFound(f"{sysfs}: gone")
        self.bus, self.address = int(bus), int(addr)

    def open(self):
        dev = self.core.find(idVendor=XINPUT_VID, idProduct=XINPUT_PID,
                             bus=self.bus, address=self.address)
        if dev is None:
            raise device.DeviceNotFound(f"{self.sysfs}: not on the bus")
        self.dev = dev
        try:
            if dev.is_kernel_driver_active(XINPUT_INTERFACE):
                dev.detach_kernel_driver(XINPUT_INTERFACE)
                self.detached = True
            self.util.claim_interface(dev, XINPUT_INTERFACE)
            self.claimed = True
        except BaseException:
            self.close()
            raise

    def write(self, data):
        n = self.dev.write(XINPUT_EP_OUT, bytes(data), timeout=500)
        if n != len(data):
            raise OSError("short USB interrupt write")

    def read(self, timeout_ms):
        try:
            return bytes(self.dev.read(XINPUT_EP_IN, XINPUT_READ_SIZE, timeout=timeout_ms))
        except self.core.USBTimeoutError:
            return None

    def close(self):
        """Release and give interface 0 back to xpad. Safe to call twice; every
        step is attempted even if an earlier one fails."""
        dev, self.dev = self.dev, None
        if dev is None:
            return
        errors = []
        if self.claimed:
            try:
                self.util.release_interface(dev, XINPUT_INTERFACE)
            except Exception as exc:         # noqa: BLE001 -- keep going
                errors.append(exc)
            self.claimed = False
        if self.detached:
            try:
                dev.attach_kernel_driver(XINPUT_INTERFACE)
                self.detached = False
            except Exception as exc:         # noqa: BLE001
                errors.append(exc)
        try:
            self.util.dispose_resources(dev)
        except Exception as exc:             # noqa: BLE001
            errors.append(exc)
        if self.detached:
            raise OSError(f"could not reattach xpad to {self.sysfs}: {errors[-1]}")


class XInputController:
    """Same surface as `Controller` -- `send`, `claim`, `close`, context
    manager -- over the XInput interrupt pipes."""

    mode = "xinput"

    def __init__(self, sysfs, link=None, verify=True, wait=0.6,
                 accept=ACCEPTED_TYPES):
        product = _read_attr(sysfs, "product") or ""
        if XINPUT_PRODUCT_TAG not in product.upper():
            raise device.DeviceNotFound(
                f"{sysfs}: product {product!r} is not a VADER4; refusing")
        self.path = XINPUT_PREFIX + sysfs
        self.link = link if link is not None else PyUsbLink(sysfs)
        self._threads = threading.RLock()
        self.info = None
        self.link.open()
        atexit.register(self.close)
        try:
            if verify:
                self.info = require(self, wait=wait, accept=accept)
        except BaseException:
            self.close()
            raise

    def close(self):
        link, self.link = getattr(self, "link", None), None
        if link is not None:
            atexit.unregister(self.close)
            link.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextlib.contextmanager
    def claim(self, timeout=device.CLAIM_TIMEOUT):
        """Within one process. Across processes the USB claim itself excludes:
        a second claimant of interface 0 gets EBUSY."""
        if not self._threads.acquire(timeout=timeout):
            raise device.DeviceBusy(f"another thread is using {self.path}")
        try:
            yield self
        finally:
            self._threads.release()

    # **The dongle drops a frame identical to the one before it**, as the Vader
    # 5's receiver does (device.REPEAT_WINDOW): measured here, an info request
    # straight after another got no answer, three times running. So a repeat
    # inside the window is preceded by a read-only separator -- the dongle
    # version request, or device info when the repeat is itself that.
    _last_frame = None
    _last_sent = 0.0

    def _separate(self, buf):
        if (bytes(buf) != self._last_frame
                or time.monotonic() - self._last_sent >= device.REPEAT_WINDOW):
            return
        cmd = X_CMD_INFO if buf[1] == X_CMD_DONGLE else X_CMD_DONGLE
        sep = xinput_command(cmd)
        self.link.write(sep)
        self._last_frame, self._last_sent = sep, time.monotonic()
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            data = self.link.read(XINPUT_POLL_MS)
            if data and len(data) > 15 and data[14] == X_MAGIC and data[15] == cmd:
                return

    def _drain(self):
        """Drop what is already queued. Bounded: the pipe streams input at
        about 1 kHz, so it is never empty for long."""
        for _ in range(16):
            if self.link.read(1) is None:
                return

    def send(self, buf, wait=0.3, until=None):
        if self.link is None:
            raise OSError(f"{self.path} is closed")
        with self.claim():
            self._drain()
            self._separate(buf)
            self.link.write(buf)
            self._last_frame, self._last_sent = bytes(buf), time.monotonic()
            replies = []
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                left = int((deadline - time.monotonic()) * 1000)
                data = self.link.read(max(1, min(XINPUT_POLL_MS, left)))
                if not data:
                    continue
                replies.append(data)
                if until is not None and until(replies):
                    break
            return replies


# -- mode-neutral exchanges ---------------------------------------------------

def _mode(ctrl):
    return getattr(ctrl, "mode", "dinput")


def command(ctrl, which, *args):
    """A request for `which` ("info", "led") framed for the controller's mode."""
    if _mode(ctrl) == "xinput":
        return xinput_command({"info": X_CMD_INFO, "led": X_CMD_READ_LED}[which], *args)
    return dinput_command({"info": D_CMD_INFO, "led": D_CMD_READ_LED}[which], *args)


def _replies(ctrl, packet, wait, until):
    parse = parse_xinput if _mode(ctrl) == "xinput" else parse_dinput
    out = []

    def check(raw):
        reply = parse(raw[-1])
        if reply is None:
            return False
        out.append(reply)
        return until(out)
    ctrl.send(packet, wait=wait, until=check)
    return out


def read_info(ctrl, wait=0.6, retries=3):
    for _ in range(retries):
        got = _replies(ctrl, command(ctrl, "info"), wait,
                       lambda rs: rs[-1].kind == "info")
        for r in got:
            if r.kind == "info":
                return r.info
    raise ProtocolError("no device-info reply")


def require(ctrl, wait=0.6, accept=ACCEPTED_TYPES):
    """Read the info and refuse anything whose device type is not in `accept`
    (105 and 85 by default; see ACCEPTED_TYPES)."""
    info = read_info(ctrl, wait=wait)
    if info.device_type not in accept:
        raise ProtocolError(
            f"device type {info.device_type} ({info.name}) is not one of "
            f"{', '.join(map(str, accept))}; refusing")
    return info


def read_lighting_raw(ctrl, cfg_id=0, wait=2.0, retries=3):
    """The whole lighting blob as the pad streams it, every parcel, unparsed."""
    return bytes(read_lighting(ctrl, cfg_id, wait, retries).blob)


def read_lighting(ctrl, cfg_id=0, wait=2.0, retries=3):
    for _ in range(retries):
        asm = Assembler(LED_PARCELS)

        def until(rs):
            if rs[-1].kind == "led":
                asm.add(rs[-1].index, rs[-1].data)
            return asm.done
        _replies(ctrl, command(ctrl, "led", cfg_id), wait, until)
        if asm.done:
            return Lighting(asm.blob)
    raise ProtocolError("lighting read did not complete")


def _start_cmd(mode):
    return X_CMD_LED_START if mode == "xinput" else D_CMD_LED_START


def _pack_cmd(mode):
    return X_CMD_LED_PACK if mode == "xinput" else D_CMD_LED_PACK


def write_lighting(ctrl, lighting, cfg_id=0, wait=3.0, retries=3, acks=None):
    """Stream a lighting blob back, one acked packet at a time.

    The start packet needs the start-ack (any number). Data parcel `k` needs a
    pack-ack numbered `k` or `k + 1`, read *after* it was sent. flydigictl
    accepts any ack >= the packet's position (start = 0, parcel k = k + 1),
    which only works if the pad numbers acks one ahead. **Measured: it does**
    -- over XInput on the type-85 pad, start acked 0, parcel k acked k + 1, 51
    acks for 51 packets, and the readback matched the blob byte for byte (a
    solid write and a restore, 2026-11-20). `k` is still accepted so DInput, not
    yet measured, has the same margin; a lower or stray number is refused.
    Each ack seen is appended to `acks` as (position, ack number) when given.

    A failure stops before the next parcel. The pad takes short blobs too
    (flydigictl's steady colour is 17 parcels), so it evidently stores parcels
    in place: a cut-off write leaves a mix, which a full rewrite replaces.
    """
    mode = _mode(ctrl)
    blob = bytes(lighting.blob)
    if len(blob) != LED_BLOB_LEN:
        raise ProtocolError(f"lighting blob is {len(blob)} bytes, expected {LED_BLOB_LEN}")
    packets = write_parcels(blob, cfg_id, mode, led=True)
    start_cmd, pack_cmd = _start_cmd(mode), _pack_cmd(mode)
    with ctrl.claim():
        for position, packet in enumerate(packets):
            if position == 0:
                def ok(r):
                    return r.kind == "ack" and r.data[:1] == bytes((start_cmd,))
            else:
                k = position - 1

                def ok(r, k=k):
                    return (r.kind == "ack" and r.data[:1] == bytes((pack_cmd,))
                            and r.index in (k, k + 1))
            for _ in range(retries):
                got = _replies(ctrl, packet, wait, lambda rs: ok(rs[-1]))
                hit = [r for r in got if ok(r)]
                if acks is not None:
                    acks.extend((position, r.index) for r in got if r.kind == "ack")
                if hit:
                    break
            else:
                raise ProtocolError(f"no ack for packet {position} of {len(packets)}")
    return len(packets)
