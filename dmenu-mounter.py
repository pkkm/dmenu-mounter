#!/usr/bin/env python3

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Displays a list of partitions using `dmenu` and mounts or unmounts
the one you select.
"""

import argparse
import os
import stat
import subprocess
import sys
from collections import OrderedDict
from enum import Enum

from tabulate import tabulate
import dbus
try:
    import notify2
    USE_NOTIFICATIONS = True
except ImportError:
    USE_NOTIFICATIONS = False

PROGRAM_NAME = os.path.basename(__file__)
if USE_NOTIFICATIONS:
    notify2.init(PROGRAM_NAME)


def is_block_device(file):
    """Return True if `file` exists and is a block device."""
    return os.path.exists(file) and stat.S_ISBLK(os.stat(file).st_mode)

def mounted_devices():
    """Return a dict with mounted block devices and their mount points.
    Example result: {"/dev/sda4": "/", "/dev/sdb1": "/mnt"}.
    """

    mounts = {}

    with open("/etc/mtab") as mtab:
        for line in mtab:
            parts = line.rstrip("\n").split(' ')

            if len(parts) < 2:
                continue

            device, mount_point, *_ = parts

            if not (os.path.exists(device) and is_block_device(device)):
                continue

            device = os.path.realpath(device)
            mounts[device] = mount_point

    return mounts

class Partition:
    """Stores data about a partition."""

    def __init__(self, device, label, mount_point, device_mtime):
        self.device = device
        self.label = label
        self.mount_point = mount_point
        self.device_mtime = device_mtime

    @property
    def mounted(self):
        return self.mount_point is not None

    def __str__(self):
        return str(self.__dict__)

def available_partitions():
    """Return a list of `Partition` objects describing the partitions in
    the system.
    """

    mounts = mounted_devices()
    partitions = []

    labels_dir = "/dev/disk/by-label"
    for label in os.listdir(labels_dir):
        device = os.path.realpath(os.path.join(labels_dir, label))
        mount_point = mounts.get(device, None)
        device_mtime = os.path.getmtime(device)

        partitions.append(Partition(
            label=label, device=device,
            mount_point=mount_point, device_mtime=device_mtime))

    return partitions

def dmenu_choose(options, prompt=None):
    """Launch dmenu for the user to choose one of `options`.

    `options` should be a dict or an OrderedDict with option names and
    their associated values.

    When the user doesn't select anything, or tries to select
    something that's not in `options`, return `None`.
    """

    dmenu_args = ["dmenu"]
    if prompt is not None:
        dmenu_args.extend(["-p", prompt])

    dmenu_input = str.join("\n", options.keys())

    dmenu = subprocess.Popen(
        dmenu_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        universal_newlines=True)

    stdout, _ = dmenu.communicate(dmenu_input)

    if dmenu.returncode == 0:
        return options.get(stdout.rstrip("\n"), None)
    else:
        return None

def default_if_none(value, default):
    """Return `value` if it's not `None`, otherwise `default`."""
    if value is not None:
        return value
    else:
        return default

def partitions_to_table(partitions):
    """Convert a list of `Partition` objects to a list of strings
    representing them as a table.
    """

    any_mounted = any(partition.mounted for partition in partitions)

    def partition_to_table_row(partition):
        row = [partition.device, partition.label]
        if any_mounted:
            row.append(default_if_none(partition.mount_point, ""))
        return row

    table = map(partition_to_table_row, partitions)

    rendered_table = tabulate(table, tablefmt="plain",
                              stralign="left", numalign="left")

    return rendered_table.split("\n")

def choose_partition(partitions, prompt):
    """Let the user choose one of `partitions` (a list of `Partition`
    objects). Return the choice or `None`.
    """
    options = OrderedDict(zip(partitions_to_table(partitions), partitions))
    return dmenu_choose(options, prompt)

def get_partitions(filter_fn=lambda _: True):
    """Return partitions on the system for which `filter_fn` returns
    `True`, ordered from most to least recent.
    """
    partitions = list(filter(filter_fn, available_partitions()))
    return sorted(partitions, key=lambda p: -p.device_mtime)

class CommandResult:
    """Stores data about the result of executing a command."""

    def __init__(self, return_code, output):
        self.return_code = return_code
        self.output = output

    @classmethod
    def run(cls, args):
        """Run a command specified by `args` and return a `CommandResult`
        object.
        """
        result = subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
        return cls(result.returncode, result.stdout)

    @property
    def success(self):
        return self.return_code == 0

    def __str__(self):
        return str(self.__dict__)

def call_privileged_command(command):
    """Execute a command as root, using `sudo` or `gksudo` if necessary.
    Return a `CommandResult` object.
    """

    # If we have root privileges, just call `command`.
    if os.geteuid() == 0:
        return CommandResult.run(command)

    # Try to use `sudo`'s cached credentials (without a password).
    if subprocess.call(["sudo", "-n", "-v"], stderr=subprocess.DEVNULL) == 0:
        return CommandResult.run(["sudo", "--"] + command)

    # Try `gksudo`.
    try:
        return CommandResult.run(["gksudo", "--"] + command)
    except FileNotFoundError:
        pass

    # When that fails and we're on a TTY, use `sudo`.
    if sys.stdin.isatty():
        return CommandResult.run(["sudo", "--"] + command)

    # Finally, when everything failed, show an error.
    message("Can't execute commands as root. Run this script as root, in a "
            "terminal, or install gksu.", MessageType.Fatal)

class MessageType(Enum):
    Info, Error, Fatal = range(3)

def message(msg, msg_type, always_print=True):
    """Show a message to the user in a desktop notification. If we can't
    display notifications or `always_print` is true, print the message
    to stdout or stderr.
    """

    # Show a notification if possible.
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

    # Print to stdout or stderr.
    if not notification_shown or always_print:
        if msg_type == MessageType.Info:
            file = sys.stdout
        else:
            file = sys.stderr

        print(msg, file=file)

    if msg_type == MessageType.Fatal:
        sys.exit(1)

def partition_to_string(partition):
    """Return a string representation of `partition` for use in
    notifications.
    """
    return partition.device + " (" + partition.label + ")"

def select_and_mount():
    """Prompt the user for a partition and mount it."""

    if os.path.ismount("/mnt"):
        message("Something is already mounted on /mnt.", MessageType.Fatal)

    selected = choose_partition(
        get_partitions(lambda partition: not partition.mounted),
        "Mount on /mnt")

    if selected is not None:
        result = call_privileged_command(
            ["mount", "--", selected.device, "/mnt"])
        if result.success:
            message("Mounted " + partition_to_string(selected) + " on /mnt.",
                    MessageType.Info)
        else:
            message("Failed to mount " + partition_to_string(selected) +
                    " on /mnt:\n" + result.output.rstrip(),
                    MessageType.Error)

def select_and_unmount():
    """Prompt the user for a mounted partition and unmount it."""

    candidates = get_partitions(
        lambda partition: (partition.mounted and
                           partition.mount_point != "/"))

    if candidates:
        selected = choose_partition(candidates, "Unmount")
        if selected is not None:
            result = call_privileged_command(
                ["umount", "--", selected.device])
            if result.success:
                message("Unmounted " + partition_to_string(selected) + ".",
                        MessageType.Info)
            else:
                message("Failed to unmount " + partition_to_string(selected) +
                        ":\n" + result.output.rstrip(),
                        MessageType.Error)
    else:
        message("No partition to unmount.", MessageType.Info)

def parse_args():
    """Parse command-line arguments and return a namespace."""

    class MyArgumentParser(argparse.ArgumentParser):
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

    parser = MyArgumentParser(
        description=__doc__,
        # Show argument defaults in help.
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    subparsers = parser.add_subparsers(dest="action", metavar="action")
    subparsers.required = True

    subparsers.add_parser("mount", help="Mount a partition.")
    subparsers.add_parser("unmount", help="Unmount a partition.")

    return parser.parse_args()

def main():
    args = parse_args()

    if args.action == "mount":
        select_and_mount()
    elif args.action == "unmount":
        select_and_unmount()

if __name__ == "__main__":
    main()
