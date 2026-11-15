#!/usr/bin/env python3
"""Unpack Android vendor_boot images (header v3-v4).

Extracts vendor_ramdisk, DTB, and (for v4) ramdisk fragments,
vendor ramdisk table, and bootconfig.
"""

import os
import re
import struct
import sys

VENDOR_BOOT_MAGIC = b"VNDRBOOT"
VENDOR_RAMDISK_HEADER_V3_SIZE = 2112
VENDOR_RAMDISK_HEADER_V4_SIZE = 2128

RAMDISK_TYPES = {
    0: "none",
    1: "platform",
    2: "recovery",
    3: "dlkm",
}


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def align(size, page_size):
    return (size + page_size - 1) // page_size * page_size


def write_part(data, out, name, offset, size):
    if size == 0:
        return

    end = offset + size
    if offset < 0 or end > len(data):
        raise ValueError(
            f"{name}: component exceeds image bounds "
            f"(offset=0x{offset:x}, size=0x{size:x}, image=0x{len(data):x})"
        )

    path = os.path.join(out, name)
    with open(path, "wb") as f:
        f.write(data[offset:end])


def safe_name(value):
    value = value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return value or "unnamed"


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <vendor_boot.img> <output_dir>")
        return 1

    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)

    with open(src, "rb") as f:
        data = f.read()

    if len(data) < 12 or data[:8] != VENDOR_BOOT_MAGIC:
        raise ValueError("Invalid vendor_boot image magic (expected VNDRBOOT)")

    version = u32(data, 8)
    if version not in (3, 4):
        raise ValueError(
            f"Unsupported vendor_boot header version: {version} "
            "(this script supports v3 and v4)"
        )

    if len(data) < VENDOR_RAMDISK_HEADER_V3_SIZE:
        raise ValueError("Truncated vendor_boot header")

    page_size = u32(data, 12)
    kernel_addr = u32(data, 16)
    ramdisk_addr = u32(data, 20)
    vendor_ramdisk_size = u32(data, 24)
    cmdline = data[28:28 + 2048].split(b"\0", 1)[0]
    tags_addr = u32(data, 2076)
    name = data[2080:2096].split(b"\0", 1)[0]
    header_size = u32(data, 2096)
    dtb_size = u32(data, 2100)
    dtb_addr = struct.unpack_from("<Q", data, 2104)[0]

    if page_size < 512 or page_size & (page_size - 1):
        raise ValueError(f"Invalid page_size: {page_size}")

    minimum_header_size = (
        VENDOR_RAMDISK_HEADER_V3_SIZE
        if version == 3 else VENDOR_RAMDISK_HEADER_V4_SIZE
    )
    if header_size < minimum_header_size or header_size > page_size:
        raise ValueError(
            f"Invalid v{version} header_size: {header_size} "
            f"(expected {minimum_header_size}..{page_size})"
        )

    info = {
        "header_version": version,
        "page_size": page_size,
        "kernel_addr": hex(kernel_addr),
        "ramdisk_addr": hex(ramdisk_addr),
        "vendor_ramdisk_size": vendor_ramdisk_size,
        "tags_addr": hex(tags_addr),
        "name": name.decode("utf-8", errors="replace"),
        "header_size": header_size,
        "dtb_size": dtb_size,
        "dtb_addr": hex(dtb_addr),
        "cmdline": cmdline.decode("utf-8", errors="replace"),
    }

    # v3/v4 layout: header page, vendor ramdisk, then DTB.
    vendor_ramdisk_offset = page_size
    dtb_offset = vendor_ramdisk_offset + align(
        vendor_ramdisk_size, page_size
    )

    info["vendor_ramdisk_offset"] = vendor_ramdisk_offset
    info["dtb_offset"] = dtb_offset

    write_part(
        data, out, "vendor_ramdisk",
        vendor_ramdisk_offset, vendor_ramdisk_size
    )
    write_part(data, out, "dtb", dtb_offset, dtb_size)

    if version == 4:
        table_size = u32(data, 2112)
        table_entry_num = u32(data, 2116)
        table_entry_size = u32(data, 2120)
        bootconfig_size = u32(data, 2124)

        if table_entry_num and table_entry_size < 108:
            raise ValueError(
                f"Invalid vendor ramdisk table entry size: {table_entry_size}"
            )

        expected_table_size = table_entry_num * table_entry_size
        if expected_table_size > table_size:
            raise ValueError(
                "Vendor ramdisk table entries exceed declared table size"
            )

        table_offset = dtb_offset + align(dtb_size, page_size)
        bootconfig_offset = table_offset + align(table_size, page_size)

        info.update({
            "vendor_ramdisk_table_size": table_size,
            "vendor_ramdisk_table_entry_num": table_entry_num,
            "vendor_ramdisk_table_entry_size": table_entry_size,
            "vendor_ramdisk_table_offset": table_offset,
            "bootconfig_size": bootconfig_size,
            "bootconfig_offset": bootconfig_offset,
        })

        write_part(
            data, out, "vendor_ramdisk_table",
            table_offset, table_size
        )
        write_part(
            data, out, "bootconfig",
            bootconfig_offset, bootconfig_size
        )

        # Each v4 table entry has:
        # size, offset, type, name[32], board_id[16] (108 bytes).
        for index in range(table_entry_num):
            entry_offset = table_offset + index * table_entry_size
            entry = data[entry_offset:entry_offset + table_entry_size]
            if len(entry) < 108:
                raise ValueError(f"Truncated vendor ramdisk table entry {index}")

            fragment_size, fragment_offset, fragment_type = (
                struct.unpack_from("<III", entry, 0)
            )
            fragment_name = safe_name(entry[12:44])
            board_id = struct.unpack_from("<16I", entry, 44)

            fragment_file = (
                f"vendor_ramdisk_fragment_{index:02d}_"
                f"{fragment_name}_{RAMDISK_TYPES.get(fragment_type, 'type' + str(fragment_type))}"
            )

            # Fragment offsets are relative to the start of the combined
            # vendor ramdisk region.
            absolute_offset = vendor_ramdisk_offset + fragment_offset
            write_part(
                data, out, fragment_file,
                absolute_offset, fragment_size
            )

            info[f"fragment_{index}_name"] = fragment_name
            info[f"fragment_{index}_type"] = fragment_type
            info[f"fragment_{index}_size"] = fragment_size
            info[f"fragment_{index}_offset"] = fragment_offset
            info[f"fragment_{index}_file"] = fragment_file
            info[f"fragment_{index}_board_id"] = ",".join(
                str(value) for value in board_id
            )

    with open(os.path.join(out, "header.txt"), "w") as f:
        for key, value in info.items():
            f.write(f"{key}: {value}\n")

    with open(os.path.join(out, "header.txt"), "r") as f:
        print(f.read())

    return 0


if __name__ == "__main__":
    sys.exit(main())
