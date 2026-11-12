"""Append the lab init and static BusyBox to an Alpine initramfs."""
import argparse
import gzip
import stat
from pathlib import Path


def archive(entries):
    data = bytearray()
    for inode, (name, mode, body) in enumerate(entries, 1):
        name_bytes = name.encode() + b"\0"
        fields = [inode, mode, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(name_bytes), 0]
        data.extend(b"070701" + "".join(f"{field:08x}" for field in fields).encode())
        data.extend(name_bytes)
        data.extend(b"\0" * (-len(data) % 4))
        data.extend(body)
        data.extend(b"\0" * (-len(data) % 4))
    return bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("alpine_initramfs", type=Path)
    parser.add_argument("busybox_static", type=Path)
    parser.add_argument("busybox_extras", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    init = Path(__file__).with_name("guest-init.sh").read_bytes().replace(b"\r\n", b"\n")
    executable = stat.S_IFREG | 0o755
    extra = archive([
        ("lab-init", executable, init),
        ("busybox", executable, args.busybox_static.read_bytes()),
        ("busybox-extras", executable, args.busybox_extras.read_bytes()),
        ("TRAILER!!!", 0, b""),
    ])
    args.output.write_bytes(args.alpine_initramfs.read_bytes() + gzip.compress(extra, mtime=0))
