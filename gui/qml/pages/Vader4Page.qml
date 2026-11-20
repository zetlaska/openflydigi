// SPDX-FileCopyrightText: 2026 zetlaska
//
// SPDX-License-Identifier: GPL-3.0-or-later

// The Vader 4 Pro: what it says about itself, and its lighting.
//
// It speaks Flydigi's older protocol, so the pad pages do not apply. Nothing
// here holds it: every button is one short session -- in XInput mode that
// briefly takes the gamepad away from xpad and always gives it back -- and the
// page reads the pad when it opens, on "Look again", after a write, and its
// battery once a minute while the page is on screen. See gui/models/vader4.py.

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import QtQuick.Dialogs as Dialogs
import org.kde.kirigami as Kirigami
import org.kde.kirigamiaddons.formcard as FormCard
import Apex5

import "../components"

Kirigami.ScrollablePage {
    id: page
    objectName: "vader4Page"
    title: "Vader 4 Pro"

    // What the next "Set colour" writes. Starts on what the pad has, once read.
    property color chosenColour: App.vader4.colour
    property int chosenBrightness: App.vader4.lightingKnown && App.vader4.brightness > 0
                                   ? App.vader4.brightness : App.vader4.brightnessMax

    actions: [
        Kirigami.Action {
            objectName: "vader4LookAgain"
            text: "Look again"
            icon.name: "view-refresh"
            enabled: !App.vader4.busy && App.vader4.selector !== ""
            onTriggered: App.vader4.refresh()
        }
    ]

    ColumnLayout {
        spacing: Kirigami.Units.largeSpacing

        Kirigami.InlineMessage {
            objectName: "vader4Error"
            Layout.fillWidth: true
            type: Kirigami.MessageType.Error
            visible: App.vader4.error !== ""
            text: App.vader4.error
        }

        Kirigami.PlaceholderMessage {
            objectName: "vader4Placeholder"
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.gridUnit * 3
            visible: !App.vader4.loaded
            icon.name: "input-gaming"
            text: App.vader4.busy ? "Reading the pad…" : "Not read yet"
            explanation: App.vader4.selector === ""
                         ? "No Vader 4 Pro is attached."
                         : "Press Look again. A pad that is asleep or switched "
                           + "off answers nothing; press a button on it first."
        }

        FormCard.FormHeader {
            visible: App.vader4.loaded
            title: "Info"
        }

        FormCard.FormCard {
            visible: App.vader4.loaded

            FormCard.FormTextDelegate {
                objectName: "vader4Name"
                text: "Device"
                description: App.vader4.deviceName
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                objectName: "vader4Firmware"
                text: "Firmware"
                description: App.vader4.firmware
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                objectName: "vader4Dongle"
                text: "Dongle firmware"
                description: App.vader4.dongleFirmware
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                objectName: "vader4Battery"
                text: "Battery"
                description: App.vader4.battery >= 0 ? App.vader4.battery + "%" : "unknown"
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                objectName: "vader4Connection"
                text: "Connection"
                description: App.vader4.connection
            }
        }

        FormCard.FormHeader {
            visible: App.vader4.loaded
            title: "Lighting (experimental)"
        }

        FormCard.FormCard {
            visible: App.vader4.loaded

            FormCard.FormTextDelegate {
                objectName: "vader4LightingNow"
                text: "On the pad now"
                description: App.vader4.lightingKnown
                             ? App.vader4.lightingMode + ", brightness "
                               + App.vader4.brightness
                               + (App.vader4.lightingMode === "Steady"
                                  ? ", " + App.vader4.colour : "")
                             : "not read"
            }
            FormCard.FormDelegateSeparator {}
            FormCard.FormTextDelegate {
                text: "Experimental"
                description: "Writes are new for this pad. Each one saves what "
                             + "it replaces as a backup first and reads the pad "
                             + "back to check."
            }
            FormCard.FormDelegateSeparator {}

            FormCard.AbstractFormDelegate {
                background: null
                hoverEnabled: false
                contentItem: RowLayout {
                    spacing: Kirigami.Units.largeSpacing
                    Controls.Label {
                        text: "Colour"
                        Layout.fillWidth: true
                    }
                    Controls.Button {
                        objectName: "vader4Swatch"
                        implicitWidth: Kirigami.Units.gridUnit * 4
                        onClicked: {
                            colourDialog.selectedColor = page.chosenColour;
                            colourDialog.open();
                        }
                        contentItem: Rectangle {
                            color: page.chosenColour
                            radius: Kirigami.Units.cornerRadius
                            border.color: Kirigami.Theme.textColor
                            border.width: 1
                        }
                    }
                }
            }
            FormCard.FormDelegateSeparator {}

            SliderRow {
                objectName: "vader4Brightness"
                label: "Brightness"
                to: App.vader4.brightnessMax
                value: page.chosenBrightness
                onMoved: (newValue) => page.chosenBrightness = newValue
            }
            FormCard.FormDelegateSeparator {}

            FormCard.FormButtonDelegate {
                objectName: "vader4SetSolid"
                text: "Set solid colour"
                icon.name: "dialog-ok-apply"
                enabled: !App.vader4.busy
                onClicked: App.vader4.setSolid(page.chosenColour, page.chosenBrightness)
            }
            FormCard.FormButtonDelegate {
                objectName: "vader4Off"
                text: "Turn the lights off"
                icon.name: "system-shutdown"
                enabled: !App.vader4.busy
                onClicked: App.vader4.turnOff()
            }
        }

        FormCard.FormHeader {
            visible: App.vader4.loaded
            title: "Backups"
        }

        FormCard.FormCard {
            visible: App.vader4.loaded

            FormCard.FormButtonDelegate {
                objectName: "vader4Backup"
                text: "Back up the lighting"
                description: "Saved in ~/.local/share/openflydigi/backups"
                icon.name: "document-save"
                enabled: !App.vader4.busy
                onClicked: App.vader4.backup()
            }
            FormCard.FormButtonDelegate {
                objectName: "vader4RestoreLatest"
                text: "Restore the latest backup"
                description: App.vader4.latestBackupName !== ""
                             ? App.vader4.latestBackupName : "no backups yet"
                icon.name: "document-revert"
                enabled: !App.vader4.busy && App.vader4.latestBackup !== ""
                onClicked: restoreConfirm.open()
            }
            FormCard.FormButtonDelegate {
                objectName: "vader4RestoreFile"
                text: "Restore from a file…"
                icon.name: "document-open"
                enabled: !App.vader4.busy
                onClicked: {
                    App.vader4.rescanBackups();
                    restoreDialog.open();
                }
            }
        }

        Controls.Label {
            objectName: "vader4Status"
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            color: Kirigami.Theme.disabledTextColor
            text: App.vader4.status
        }
    }

    Dialogs.ColorDialog {
        id: colourDialog
        objectName: "vader4ColourDialog"
        title: "Vader 4 Pro lighting colour"
        onAccepted: page.chosenColour = selectedColor
    }

    Dialogs.FileDialog {
        id: restoreDialog
        objectName: "vader4RestoreDialog"
        title: "Restore Vader 4 Pro lighting"
        fileMode: Dialogs.FileDialog.OpenFile
        currentFolder: App.vader4.backupFolder
        selectedFile: App.vader4.latestBackup !== ""
                      ? "file://" + App.vader4.latestBackup : ""
        nameFilters: ["Vader 4 lighting backup (vader4-*.bin)", "All files (*)"]
        onAccepted: App.vader4.restore(selectedFile)
    }

    Kirigami.PromptDialog {
        id: restoreConfirm
        objectName: "vader4RestoreConfirm"
        title: "Restore lighting?"
        subtitle: "Write " + App.vader4.latestBackupName + " to the pad. What is "
                  + "on it now is backed up first."
        standardButtons: Kirigami.Dialog.Cancel
        customFooterActions: [
            Kirigami.Action {
                text: "Restore"
                icon.name: "document-revert"
                onTriggered: {
                    App.vader4.restore("");
                    restoreConfirm.close();
                }
            }
        ]
    }
}
