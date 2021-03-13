#!/usr/bin/env python3
# pylint: disable=missing-docstring,too-few-public-methods

"""Displays a list of devices using `dmenu` and mounts or unmounts the one the
user selects.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional
from enum import Enum

import dbus # Dependency of the notify2 library.
import tabulate
try:
    import notify2
    USE_NOTIFICATIONS = True
except ImportError:
    USE_NOTIFICATIONS = False

PROGRAM_NAME = "dmenu-mounter"
if USE_NOTIFICATIONS:
    notify2.init(PROGRAM_NAME)

class MessageType(Enum):
    Info, Error, Fatal = range(3)

def message(msg, msg_type, always_print=True):
    """Show a message to the user in a desktop notification. If we can't display
    notifications or `always_print` is true, print the message to stdout or
    stderr.
    """

    notification_shown = False
    if USE_NOTIFICATIONS:
        try:
            notification = notify2.Notification(PROGRAM_NAME, msg)
            notification.set_urgency({
                # Levels: URGENCY_LOW, URGENCY_NORMAL, URGENCY_CRITICAL.
                MessageType.Info: notify2.URGENCY_LOW,
                MessageType.Error: notify2.URGENCY_NORMAL,
                MessageType.Fatal: notify2.URGENCY_NORMAL,
            }[msg_type])
            notification.show()
            notification_shown = True
        except dbus.exceptions.DBusException:
            pass

    if not notification_shown or always_print:
        print(msg, file=sys.stderr)

    if msg_type == MessageType.Fatal:
        sys.exit(1)

@dataclass
class Device:
    """Data about a block device."""

    path: str
    filesystem: Optional[str]
    label: Optional[str]
    uuid: Optional[str]
    mountpoint: Optional[str]
    size: str
    mtime: float

    @property
    def mounted(self):
        return self.mountpoint is not None

    def to_short_string(self):
        """Return a string representation for use in notifications."""
        if self.label is None:
            return self.path
        return "{} ({})".format(self.path, self.label)

def handled_devices():
    """Return a list of `Device` objects representing all block devices we can
    do something with, ordered from the most recent mtime.
    """

    process = subprocess.run(
        ["lsblk", "--json", "--list", "--output",
         "NAME,PATH,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,SIZE"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)

    if process.returncode != 0:
        message(
            "Can't get block devices with lsblk:\n{}".format(process.stdout),
            MessageType.Fatal)

    devices_json = json.loads(process.stdout)

    result = []
    for device in devices_json["blockdevices"]:
        # Skip disks, extended partitions and swap.
        if device["fstype"] is None or device["fstype"] == "swap":
            continue

        try:
            mtime = os.path.getmtime(device["path"])
        except OSError:
            mtime = None

        result.append(Device(
            path=device["path"],
            filesystem=device["fstype"],
            label=device["label"],
            uuid=device["uuid"],
            mountpoint=device["mountpoint"],
            size=device["size"],
            mtime=mtime))

    return sorted(result, key=lambda d: d.mtime, reverse=True)

def prepare_table(table, delete_none_columns=True):
    """Assuming that `table` is a list of lists representing a table, return the
    table with columns that contain only `None` removed, and remaining cells
    that are `None` replaced with the empty string.
    """

    if not table:
        return table

    n_columns = len(table[0])

    if delete_none_columns:
        should_delete_column = [True] * n_columns
        for i_column in range(n_columns):
            for row in table:
                if row[i_column] is not None:
                    should_delete_column[i_column] = False
    else:
        should_delete_column = [False] * n_columns

    result = []
    for row in table:
        new_row = []
        for i_column, cell in enumerate(row):
            if should_delete_column[i_column]:
                continue

            if cell is None:
                new_row.append("")
            else:
                new_row.append(cell)

        result.append(new_row)

    return result

def devices_to_table(devices):
    """Convert a list of `Device` objects to a list of strings presenting them
    as a table.
    """

    table = prepare_table([
        [d.path, d.label, d.mountpoint, d.filesystem, d.size]
        for d in devices])

    rendered_table = tabulate.tabulate(
        table, tablefmt="plain", stralign="left", numalign="left")

    return rendered_table.split("\n")

def dmenu_choose(options, prompt=None):
    """Launch dmenu for the user to choose one of `options`.

    `options` should be a dict or OrderedDict with option names and their
    associated values.

    When the user doesn't select anything or tries to select something that's
    not in `options`, return `None`.
    """

    args = ["dmenu"]
    if prompt is not None:
        args.extend(["-p", prompt])

    process = subprocess.run(
        args,
        input=str.join("\n", options.keys()),
        stdout=subprocess.PIPE,
        encoding="utf-8")

    if process.returncode == 0:
        return options.get(process.stdout.rstrip("\n"), None)
    return None

def choose_device(devices, prompt):
    """Let the user choose one of `devices` (a list of `Device` objects).
    Return the choice or `None`.
    """
    options = OrderedDict(zip(devices_to_table(devices), devices))
    return dmenu_choose(options, prompt)

def call_privileged_command(command):
    """Execute a command as root, using `sudo` or `pkexec` if necessary. Return
    a `subprocess.CompletedProcess` whose `stderr` member contains the stdout
    and stderr of the command.
    """

    def run(args):
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)

    # If we have root privileges, just call `command`.
    if os.geteuid() == 0:
        return run(command)

    # Try to use `sudo`'s cached credentials (without a password).
    if subprocess.call(["sudo", "-n", "-v"], stderr=subprocess.DEVNULL) == 0:
        return run(["sudo", "--"] + command)

    # Try `pkexec`.
    try:
        program_path = shutil.which(command[0])
        if program_path is not None:
            return run(["pkexec", program_path] + command[1:])
    except FileNotFoundError:
        pass

    # When that fails and we're on a TTY, use `sudo`.
    if sys.stdin.isatty():
        return run(["sudo", "--"] + command)

    # Finally, when everything failed, show an error.
    message(
        "Can't execute commands as root. Run this script as root, in a " +
        "terminal, or install pkexec.",
        MessageType.Fatal)

    return None

def select_and_mount():
    """Prompt the user for a device that's not mounted and mount it."""

    if os.path.ismount("/mnt"):
        message("Something is already mounted on /mnt", MessageType.Fatal)

    candidates = [d for d in handled_devices() if not d.mounted]
    if not candidates:
        message("No device to mount", MessageType.Info)
        return

    selected = choose_device(candidates, "Mount on /mnt")
    if selected is None:
        return

    result = call_privileged_command(
        ["mount", "--", selected.path, "/mnt"])
    if result.returncode == 0:
        message(
            "Mounted {} on /mnt".format(selected.to_short_string()),
            MessageType.Info)
    else:
        message(
            "Failed to mount {} on /mnt:\n{}".format(
                selected.to_stort_string(), result.stdout.rstrip()),
            MessageType.Error)

def select_and_unmount():
    """Prompt the user for a mounted device and unmount it."""

    candidates = [
        d for d in handled_devices()
        if d.mounted and d.mountpoint != "/"]
    if not candidates:
        message("No device to unmount", MessageType.Info)
        return

    selected = choose_device(candidates, "Unmount")
    if selected is None:
        return

    result = call_privileged_command(
        ["umount", "--", selected.path])
    if result.returncode == 0:
        message(
            "Unmounted {}".format(selected.to_short_string()),
            MessageType.Info)
    else:
        message(
            "Failed to unmount {}:\n{}".format(
                selected.to_short_string(), result.stdout.rstrip()),
            MessageType.Error)

def parse_args():
    """Parse command-line arguments and return a namespace."""

    class MessageArgumentParser(argparse.ArgumentParser):
        """Like `argparse.ArgumentParser`, but uses `message` instead of
        printing to stdout or stderr.
        """

        def _print_message(self, msg, file=sys.stderr):
            if not msg:
                return

            if file == sys.stderr:
                message_type = MessageType.Error
            else:
                message_type = MessageType.Info

            message(msg.rstrip(), message_type)

    parser = MessageArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    subparsers = parser.add_subparsers(dest="action", metavar="action")
    subparsers.required = True

    subparsers.add_parser("mount", help="mount a device")
    subparsers.add_parser("unmount", help="unmount a device")

    return parser.parse_args()

def main():
    args = parse_args()

    if args.action == "mount":
        select_and_mount()
    else:
        select_and_unmount()

if __name__ == "__main__":
    main()
