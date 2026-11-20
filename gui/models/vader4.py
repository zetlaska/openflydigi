# SPDX-FileCopyrightText: 2026 zetlaska
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""The Vader 4 Pro's page: what it says about itself, and its lighting.

**Nothing here holds the pad.** The 37d7 pads keep a handle open on the shared
worker; this one cannot, because in XInput mode a session detaches xpad from
the gamepad interface, and a held session is a gamepad that does not work.
So every action is one short session on a thread of its own -- open, read or
write, close, xpad reattached -- through `flydigi.vader4`, the same functions
the command-line tool uses. Two sessions never overlap: `vader4.SessionLock`
is taken by every one of them, in this window or any other process, and
flydigid never opens this pad at all.

**No constant polling.** The pad is read when its page is opened, when
"Look again" is pressed, and after a write (the write's own readback). While
the page is on screen the battery is read once a minute, info only; nothing is
read while it is not.

Lighting writes are experimental, and say so on the page. Each one saves what
it replaces as a backup first and reads the pad back to compare.
"""
import os

from PySide6.QtCore import Property, QObject, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtQml import QmlElement

from flydigi import device, vader4

QML_IMPORT_NAME = "Apex5"
QML_IMPORT_MAJOR_VERSION = 1

BATTERY_MS = 60_000

MODE_LABELS = {"xinput": "XInput", "dinput": "DInput"}
LIGHTING_LABELS = {name: name.capitalize() for name in vader4.MODE_NAMES.values()}


def explain(exc):
    """A failed session, as a sentence for the page."""
    if isinstance(exc, device.DeviceBusy):
        return "Another OpenFlydigi session is using the pad. Try again in a moment."
    if isinstance(exc, device.DeviceNotFound):
        return f"The pad is not attached ({exc})."
    if isinstance(exc, PermissionError) or getattr(exc, "errno", None) == 13:
        return ("No permission to open the pad. Install the udev rules from "
                "udev/ and replug the dongle.")
    if isinstance(exc, vader4.ProtocolError):
        return f"The pad did not answer as expected: {exc}"
    return f"{type(exc).__name__}: {exc}"


class Job(QThread):
    """One session, start to finish. Short-lived, so safe to wait on."""

    done = Signal(str, object, str)   # what, result, error

    def __init__(self, what, fn, parent=None):
        super().__init__(parent)
        self.what, self.fn = what, fn

    def run(self):
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
            self.done.emit(self.what, None, explain(exc))
            return
        self.done.emit(self.what, result, "")


@QmlElement
class Vader4Model(QObject):
    """What the Vader 4 Pro page binds to."""

    changed = Signal()
    busyChanged = Signal()
    pollingChanged = Signal()
    backupsChanged = Signal()
    failed = Signal(str)

    def __init__(self, parent=None, backend=None, inline=False, backup_dir=None):
        super().__init__(parent)
        # Injectable so the tests drive a fake instead of a USB device, and can
        # run a job inline rather than on a thread.
        self._backend = backend if backend is not None else vader4
        self._inline = inline
        self._backup_dir = backup_dir
        self._selector = ""
        self._job = None
        self._state = {}
        self._status = ""
        self._error = ""
        self._backups = []
        self._battery = QTimer(self)
        self._battery.setInterval(BATTERY_MS)
        self._battery.timeout.connect(self._battery_tick)
        self._scan_backups()

    # -- which pad ---------------------------------------------------------

    @Slot(str)
    def setSelector(self, selector):
        if selector == self._selector:
            return
        self._selector = selector
        self._state = {}
        self._error = ""
        self._status = ""
        self.changed.emit()
        if self.polling:
            self.refresh()

    def _path(self):
        """The pad's path out of its selector -- always `path:` for this pad."""
        kind, _, value = self._selector.partition(":")
        return value if kind == "path" else self._selector

    # -- sessions ----------------------------------------------------------

    def _run(self, what, status, fn):
        if self._job is not None or not self._selector:
            return False
        self._status = status
        self._error = ""
        self.changed.emit()
        if self._inline:
            try:
                result, error = fn(), ""
            except Exception as exc:  # noqa: BLE001
                result, error = None, explain(exc)
            self._finished(what, result, error)
            return True
        self._job = Job(what, fn, self)
        self._job.done.connect(self._finished)
        self._job.start()
        self.busyChanged.emit()
        return True

    def _finished(self, what, result, error):
        job, self._job = self._job, None
        if job is not None:
            job.wait()
            job.deleteLater()
        if error:
            self._error = error
            self._status = ""
            self.failed.emit(error)
        else:
            self._state.update(result or {})
            self._status = {
                "read": "Read from the pad.",
                "battery": "Read from the pad.",
                "backup": "Backed up to " + os.path.basename(result.get("backup", "")),
                "write": ("Written, and the pad reads back what was sent."
                          if result.get("matched") else
                          "Written, but the pad reads back something different."),
            }.get(what, "")
            if what == "write" and not result.get("matched"):
                self._error = ("The readback differs from what was written; "
                               "the previous lighting is in "
                               + os.path.basename(result.get("backup", "")))
            if what in ("backup", "write"):
                self._scan_backups()
        self.changed.emit()
        self.busyChanged.emit()

    @Slot()
    def refresh(self):
        """Look again: info, dongle and lighting, one session."""
        path = self._path()
        self._run("read", "Reading the pad…",
                  lambda: self._backend.snapshot(path, lighting=True))

    def _battery_tick(self):
        path = self._path()
        self._run("battery", "Reading the battery…",
                  lambda: self._backend.snapshot(path, lighting=False))

    @Slot(QColor, int)
    def setSolid(self, colour, brightness):
        rgb = (colour.red(), colour.green(), colour.blue())
        self._write("solid", colour=rgb, brightness=int(brightness))

    @Slot(str, int)
    def setSolidHex(self, text, brightness):
        self.setSolid(QColor(text), brightness)

    @Slot()
    def turnOff(self):
        self._write("off")

    @Slot()
    def backup(self):
        path, directory = self._path(), self._backup_dir
        self._run("backup", "Backing up the lighting…",
                  lambda: self._backend.backup_lighting(path, backup_dir=directory))

    @Slot("QVariant")
    def restore(self, file=""):
        """Restore a backup file; the latest one when given none."""
        if isinstance(file, QUrl):
            file = file.toLocalFile()
        file = str(file or "")
        if file.startswith("file:"):
            file = QUrl(file).toLocalFile()
        file = file or self.latestBackup
        if not file:
            self._error = "There is no backup to restore."
            self.changed.emit()
            return
        backend = self._backend

        def load():
            return backend.load_backup(file)
        self._write("restore", blob_loader=load)

    def _write(self, action, colour=None, brightness=None, blob_loader=None):
        path, backend, directory = self._path(), self._backend, self._backup_dir

        def go():
            blob = blob_loader() if blob_loader else None
            return backend.apply_lighting(path, action, colour=colour,
                                          brightness=brightness, blob=blob,
                                          backup_dir=directory)
        self._run("write", "Writing the lighting…", go)

    # -- backups -----------------------------------------------------------

    def _scan_backups(self):
        try:
            found = self._backend.list_backups(self._backup_dir)
        except OSError:
            found = []
        if found != self._backups:
            self._backups = found
            self.backupsChanged.emit()

    @Slot()
    def rescanBackups(self):
        self._scan_backups()

    @Property(list, notify=backupsChanged)
    def backups(self):
        return list(self._backups)

    @Property(str, notify=backupsChanged)
    def latestBackup(self):
        return self._backups[0] if self._backups else ""

    @Property(str, notify=backupsChanged)
    def latestBackupName(self):
        return os.path.basename(self.latestBackup)

    @Property(QUrl, constant=True)
    def backupFolder(self):
        return QUrl.fromLocalFile(self._backup_dir or self._backend.BACKUP_DIR)

    # -- polling -----------------------------------------------------------

    @Property(bool, notify=pollingChanged)
    def polling(self):
        """True while the page is on screen: read on open, battery each minute."""
        return self._battery.isActive()

    @polling.setter
    def polling(self, value):
        if bool(value) == self.polling:
            return
        if value:
            self._battery.start()
            self.refresh()
        else:
            self._battery.stop()
        self.pollingChanged.emit()

    def wait(self, msecs=5000):
        """For shutdown: a running QThread that loses its last reference is a
        qFatal. A session is a second or two at most."""
        try:
            self.polling = False
        except RuntimeError:
            pass
        if self._job is not None:
            self._job.wait(msecs)

    # -- what the page shows -----------------------------------------------

    def _get(self, key, default=None):
        value = self._state.get(key)
        return default if value is None else value

    @Property(str, notify=changed)
    def selector(self):
        return self._selector

    @Property(bool, notify=busyChanged)
    def busy(self):
        return self._job is not None

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=changed)
    def error(self):
        return self._error

    @Property(bool, notify=changed)
    def loaded(self):
        return "device_type" in self._state

    @Property(bool, notify=changed)
    def lightingKnown(self):
        return "lighting_mode" in self._state

    @Property(str, notify=changed)
    def deviceName(self):
        if "device_type" not in self._state:
            return ""
        name = self._state.get("name") or "Vader 4 Pro"
        tag = f"type {self._state['device_type']}"
        return name if tag in name else f"{name} ({tag})"

    @Property(str, notify=changed)
    def firmware(self):
        return self._get("firmware", "")

    @Property(str, notify=changed)
    def dongleFirmware(self):
        if "device_type" not in self._state:
            return ""
        return self._get("dongle_firmware", "none answered (cable?)")

    @Property(int, notify=changed)
    def battery(self):
        return int(self._get("battery", -1))

    @Property(str, notify=changed)
    def connection(self):
        mode = MODE_LABELS.get(self._get("mode", ""), self._get("mode", ""))
        link = self._get("connection", "")
        return " · ".join(part for part in (mode, link) if part)

    @Property(str, notify=changed)
    def lightingMode(self):
        name = self._get("lighting_mode_name", "")
        return LIGHTING_LABELS.get(name, name)

    @Property(int, notify=changed)
    def brightness(self):
        return int(self._get("brightness", 0))

    @Property(int, constant=True)
    def brightnessMax(self):
        return vader4.BRIGHTNESS_MAX

    @Property(str, notify=changed)
    def colour(self):
        return self._get("colour", "#000000")
