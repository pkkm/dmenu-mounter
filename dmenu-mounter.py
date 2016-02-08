#!/usr/bin/env python3

# Dependencies (on Debian:)
#   apt-get install gksu python3-tabulate python3-notify2 libnotify-dev

import os
import stat
import subprocess
import sys
from collections import OrderedDict
from enum import Enum

PROGRAM_NAME = os.path.basename(__file__)

from tabulate import tabulate

# Try to use notifications.
USE_NOTIFICATIONS = True
try:
    import notify2
    notify2.init(PROGRAM_NAME)
except:
    USE_NOTIFICATIONS = False


class Partition:
    """A class representing data about a partition."""

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
    if prompt != None:
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

def render_partitions(partitions, show_mount_points=True):
    """Convert a list of `Partition` objects to a list of strings
    representing them as a table.
    """

    any_mounted = any(partition.mounted for partition in partitions)

    def partition_to_table_row(partition):
        row = []
        if any_mounted:
            row.append(default_if_none(partition.mount_point, ""))
        row.append(partition.device)
        row.append(partition.label)
        return row

    table = map(partition_to_table_row, partitions)

    rendered_table = tabulate(table, tablefmt="plain",
                              stralign="left", numalign="left")

    return rendered_table.split("\n")

def choose_partition(partitions, prompt):
    """Ask the user to choose one of `partitions` (a list of `Partition`
    objects). Return the choice, or `None` if something goes wrong.
    """
    options = OrderedDict(zip(render_partitions(partitions), partitions))
    return dmenu_choose(options, prompt)

def call_privileged_command(command):
    """Execute a command as root, using `sudo` or `gksudo` if necessary.
    Return the command's exit code.
    """

    # If we have root privileges, just call `command`.
    if os.geteuid() == 0:
        return subprocess.call(command)

    # Try to use `sudo`'s cached credentials (without a password).
    if subprocess.call(["sudo", "-n", "-v"], stderr=subprocess.DEVNULL) == 0:
        return subprocess.call(["sudo", "--"] + command)

    # Try `gksudo`.
    try:
        return subprocess.call(["gksudo", "--"] + command)
    except FileNotFoundError:
        pass

    # When that fails and we're on a TTY, use `sudo`.
    if os.stdin.isatty():
        return subprocess.call(["sudo", "--"] + command)

    # Finally, when everything failed, show an error.
    message("Can't execute commands as root. Run this script as root, in a "
            "terminal, or install gksu.", MessageType.Fatal)

class MessageType(Enum):
    Info, Error, Fatal = range(3)

def message(msg, msg_type):
    """Show a message to the user (as a desktop notification if possible,
    otherwise on the terminal).
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

    # Otherwise, print to terminal.
    if not notification_shown:
        file = sys.stdout if msg_type == MessageType.Info else sys.stderr
        print(msg, file=file)

    if msg_type == MessageType.Fatal:
        sys.exit(1)


if __name__ == "__main__":
    args = sys.argv[1:]

    if args == ["--mount"]:
        if os.path.ismount("/mnt"):
            message("Something is already mounted on /mnt.", MessageType.Fatal)

        candidates = filter(lambda partition: not partition.mounted,
                            available_partitions())
        candidates = sorted(list(candidates),
                            key=lambda partition: -partition.device_mtime)

        selected = choose_partition(candidates, "Mount on /mnt")
        if selected is not None:
            result = call_privileged_command(
                ["mount", "--", selected.device, "/mnt"])
            if result == 0:
                message("Mounted " + selected.device + " on /mnt.",
                        MessageType.Info)
            else:
                message("Failed to mount " + selected.device + " on /mnt.",
                        MessageType.Error)

    elif args == ["--umount"]:
        candidates = filter(lambda partition: (partition.mounted and
                                               partition.mount_point != "/"),
                            available_partitions())
        candidates = sorted(list(candidates),
                            key=lambda partition: -partition.device_mtime)

        if candidates:
            selected = choose_partition(candidates, "Unmount")
            if selected is not None:
                result = call_privileged_command(
                    ["umount", "--", selected.device])
                if result == 0:
                    message("Unmounted " + selected.device + ".",
                            MessageType.Info)
                else:
                    message("Failed to unmount " + selected.device + ".",
                            MessageType.Error)
        else:
            message("No partition to unmount.", MessageType.Info)

    else:
        message("USAGE: " + __file__ + " {--mount | --umount}",
                MessageType.Fatal)
