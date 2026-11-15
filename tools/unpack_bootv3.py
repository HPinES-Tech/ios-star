#!/usr/bin/env python3
"""Unpack Android boot images (header v0-v4) into components + header.txt."""

import os
import struct
import sys

PAGE_SIZE_V3 = 4096


def read_u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def rnd(size, page_size):
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

    with open(os.path.join(out, name), "wb") as f:
        f.write(data[offset:end])


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <boot.img> <output_dir>")
        return 1

    src, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)

    with open(src, "rb") as f:
        b = f.read()

    if b[:8] != b"ANDROID!":
        raise ValueError("Invalid Android boot image magic")

    if len(b) < 44:
        raise ValueError("Boot image is too small to contain a complete header")

    ver = read_u32(b, 40)
    info = {"header_version": ver}

    if ver <= 2:
        # Legacy boot image header (v0-v2): preserve the original
        # unpack_bootv2.py field layout and component extraction logic.
        (ks, ka, rs, ra, ss, sa, ta, ps, _, osv) = struct.unpack_from(
            "<10I", b, 8
        )

        if ps == 0 or ps & (ps - 1):
            raise ValueError(f"Invalid legacy page_size: {ps}")

        cmd = (
            b[64:64 + 512].split(b"\0")[0]
            + b[608:608 + 1024].split(b"\0")[0]
        )

        info.update({
            "kernel_size": ks,
            "kernel_addr": hex(ka),
            "ramdisk_size": rs,
            "ramdisk_addr": hex(ra),
            "second_size": ss,
            "tags_addr": hex(ta),
            "page_size": ps,
            "os_version": hex(osv),
            "name": b[48:64].split(b"\0")[0].decode(errors="replace"),
            "cmdline": cmd.decode(errors="replace"),
        })

        off = ps

        for name, size in (
            ("kernel", ks),
            ("ramdisk", rs),
            ("second", ss),
        ):
            info[name + "_offset"] = off
            write_part(b, out, name, off, size)
            off += rnd(size, ps)

        recovery_size = 0
        recovery_offset = 0

        if ver >= 1:
            recovery_size, recovery_offset, header_size = struct.unpack_from(
                "<IQI", b, 1632
            )
            info["header_size"] = header_size
            info["recovery_dtbo_size"] = recovery_size
            info["recovery_dtbo_offset"] = recovery_offset

            if recovery_size:
                write_part(
                    b, out, "recovery_dtbo", recovery_offset, recovery_size
                )
                off = max(off, recovery_offset + rnd(recovery_size, ps))

        if ver >= 2:
            dtb_size, dtb_addr = struct.unpack_from("<IQ", b, 1648)
            info["dtb_size"] = dtb_size
            info["dtb_addr"] = hex(dtb_addr)
            info["dtb_offset"] = off
            write_part(b, out, "dtb", off, dtb_size)

    elif ver in (3, 4):
        # Header v3/v4 has a fixed 4096-byte page and a different layout.
        if len(b) < PAGE_SIZE_V3:
            raise ValueError("Truncated v3/v4 boot image header")

        ks = read_u32(b, 8)
        rs = read_u32(b, 12)
        osv = read_u32(b, 16)
        hs = read_u32(b, 20)

        minimum_header_size = 1580 if ver == 3 else 1584
        if hs < minimum_header_size or hs > PAGE_SIZE_V3:
            raise ValueError(f"Invalid v{ver} header_size: {hs}")

        cmd = b[44:44 + 1536].split(b"\0")[0]

        info.update({
            "kernel_size": ks,
            "ramdisk_size": rs,
            "os_version": hex(osv),
            "header_size": hs,
            "page_size": PAGE_SIZE_V3,
            "cmdline": cmd.decode(errors="replace"),
        })

        off = PAGE_SIZE_V3

        info["kernel_offset"] = off
        write_part(b, out, "kernel", off, ks)
        off += rnd(ks, PAGE_SIZE_V3)

        info["ramdisk_offset"] = off
        write_part(b, out, "ramdisk", off, rs)
        off += rnd(rs, PAGE_SIZE_V3)

        if ver == 4:
            signature_size = read_u32(b, 1580)
            info["boot_signature_size"] = signature_size
            info["boot_signature_offset"] = off
            write_part(
                b, out, "boot_signature", off, signature_size
            )

        info["note"] = (
            "For header v3/v4, the device tree is normally supplied "
            "through vendor_boot.img, not as a DTB component in boot.img."
        )

    else:
        raise ValueError(f"Unsupported Android boot header version: {ver}")

    with open(os.path.join(out, "header.txt"), "w") as f:
        for key, value in info.items():
            f.write(f"{key}: {value}\n")

    with open(os.path.join(out, "header.txt"), "r") as f:
        print(f.read())

    return 0


if __name__ == "__main__":
    sys.exit(main())
