import io
import struct
import sys
from pathlib import Path

import requests


BLOCK = 2048
CACHE = 4 * 1024 * 1024


class RangeFile(io.RawIOBase):
    def __init__(self, url: str):
        self.session = requests.Session()
        self.position = 0
        self.cache_start = -1
        self.cache_data = b""
        response = self.session.get(
            url,
            headers={"Range": "bytes=0-0"},
            allow_redirects=True,
            timeout=30,
        )
        response.raise_for_status()
        if response.status_code != 206:
            raise RuntimeError(f"Range is not supported: HTTP {response.status_code}")
        content_range = response.headers.get("Content-Range", "")
        if "/" not in content_range:
            raise RuntimeError("Missing Content-Range")
        self.size = int(content_range.rsplit("/", 1)[1])
        self.url = response.url
        print(f"ISO size: {self.size:,} bytes")
        print(f"Final URL: {self.url[:120]}...")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            new_position = offset
        elif whence == io.SEEK_CUR:
            new_position = self.position + offset
        elif whence == io.SEEK_END:
            new_position = self.size + offset
        else:
            raise ValueError(f"Invalid whence: {whence}")
        if new_position < 0:
            raise ValueError("Negative seek")
        self.position = new_position
        return self.position

    def _fill_cache(self):
        start = self.position
        end = min(start + CACHE - 1, self.size - 1)
        response = self.session.get(
            self.url,
            headers={"Range": f"bytes={start}-{end}"},
            timeout=60,
        )
        response.raise_for_status()
        if response.status_code != 206:
            raise IOError(f"Range request failed: HTTP {response.status_code}")
        self.cache_start = start
        self.cache_data = response.content
        print(f"Range GET: {start}-{end} ({len(response.content):,} bytes)")

    def read(self, size=-1):
        if self.position >= self.size:
            return b""
        if size is None or size < 0:
            size = self.size - self.position
        size = min(size, self.size - self.position)
        output = bytearray()
        while size:
            cache_end = self.cache_start + len(self.cache_data)
            if not self.cache_start <= self.position < cache_end:
                self._fill_cache()
                cache_end = self.cache_start + len(self.cache_data)
            offset = self.position - self.cache_start
            count = min(size, len(self.cache_data) - offset)
            output.extend(self.cache_data[offset : offset + count])
            self.position += count
            size -= count
        return bytes(output)

    def block(self, lba):
        self.seek(lba * BLOCK)
        data = self.read(BLOCK)
        if len(data) != BLOCK:
            raise IOError(f"Short block at LBA {lba}: {len(data)} bytes")
        return data


def u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def u64(data, offset):
    return struct.unpack_from("<Q", data, offset)[0]


def extent(data, offset):
    length = u32(data, offset) & 0x3FFFFFFF
    kind = u32(data, offset) >> 30
    return {"lba": u32(data, offset + 4), "length": length, "kind": kind}


def descriptor_tag(block):
    tag = u16(block, 0)
    checksum = sum(block[index] for index in range(16) if index != 4) & 0xFF
    return tag if checksum == block[4] else -1


def entity_id(block, offset):
    return block[offset + 1 : offset + 24].rstrip(b"\x00").decode("ascii", "replace")


def cs0(data):
    data = bytes(data)
    if not data:
        return ""
    compression = data[0]
    payload = data[1:]
    if compression == 8:
        return payload.decode("latin-1", "replace")
    if compression == 16:
        return payload[: len(payload) // 2 * 2].decode("utf-16-be", "replace")
    return payload.decode("latin-1", "replace")


def long_ad(data, offset):
    value = u32(data, offset)
    return {
        "kind": value >> 30,
        "length": value & 0x3FFFFFFF,
        "lba": u32(data, offset + 4),
        "partition": u16(data, offset + 8),
    }


def file_entry(data, partition_ref=None):
    tag = descriptor_tag(data)
    if tag not in (261, 266):
        raise RuntimeError(f"Expected File Entry, got tag {tag}")
    if tag == 261:
        ea_offset, ad_offset = 168, 176
    else:
        ea_offset, ad_offset = 208, 216
    l_ea = u32(data, ea_offset)
    l_ad = u32(data, ea_offset + 4)
    flags = u16(data, 34)
    ad_type = flags & 7
    entry = {
        "tag": tag,
        "file_type": data[27],
        "flags": flags,
        "length": u64(data, 56),
        "l_ea": l_ea,
        "l_ad": l_ad,
        "ad_type": ad_type,
        "inline": ad_type == 3,
        "ads": [],
    }
    if entry["inline"]:
        entry["inline_data"] = data[ad_offset : ad_offset + l_ad]
        return entry
    if ad_type == 0:
        ad_size = 8
    elif ad_type == 1:
        ad_size = 16
    elif ad_type == 2:
        ad_size = 20
    else:
        raise RuntimeError(f"Unsupported allocation descriptor type {ad_type}")
    for offset in range(ad_offset, ad_offset + l_ad, ad_size):
        if ad_type == 1:
            entry["ads"].append(long_ad(data, offset))
        elif ad_type == 0:
            value = u32(data, offset)
            entry["ads"].append({"kind": value >> 30, "length": value & 0x3FFFFFFF, "lba": u32(data, offset + 4), "partition": partition_ref})
        else:
            value = u32(data, offset)
            entry["ads"].append({"kind": value >> 30, "length": value & 0x3FFFFFFF, "lba": u32(data, offset + 12), "partition": u16(data, offset + 16)})
    return entry


def read_directory(remote, base_lba, entry):
    if entry["inline"]:
        return entry["inline_data"][: entry["length"]]
    if not entry["ads"]:
        return b""
    ad = entry["ads"][0]
    start_lba = base_lba(ad["partition"]) + ad["lba"]
    size = ad["length"]
    data = b"".join(remote.block(start_lba + index) for index in range((size + BLOCK - 1) // BLOCK))
    return data[:size]


def directory_entries(data):
    entries = []
    cursor = 0
    while cursor + 38 <= len(data):
        if descriptor_tag(data[cursor : cursor + BLOCK]) != 257:
            break
        name_len = data[cursor + 19]
        implementation_len = u16(data, cursor + 36)
        used = 4 * ((38 + implementation_len + name_len + 3) // 4)
        if used < 40 or cursor + used > len(data):
            break
        name = cs0(data[cursor + 38 + implementation_len : cursor + 38 + implementation_len + name_len])
        characteristic = data[cursor + 18]
        if name and not (characteristic & 0x08):
            entries.append({
                "name": name,
                "characteristic": characteristic,
                "icb": long_ad(data, cursor + 20),
            })
        cursor += used
    return entries


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: conda run -n base python udf_probe.py movie.strm")
    strm = Path(sys.argv[1])
    url = strm.read_text(encoding="utf-8-sig").strip()
    remote = RangeFile(url)

    print("\nAnchor candidates:")
    anchors = []
    for lba in (256, remote.size // BLOCK - 1, remote.size // BLOCK - 257):
        block = remote.block(lba)
        tag = descriptor_tag(block)
        print(f"  LBA {lba}: tag {tag}")
        if tag == 2:
            anchors.append((lba, block))
    if not anchors:
        raise RuntimeError("No Anchor Volume Descriptor Pointer")

    anchor = anchors[0][1]
    main_vds = extent(anchor, 16)
    reserve_vds = extent(anchor, 24)
    print(f"Main VDS: {main_vds}")
    print(f"Reserve VDS: {reserve_vds}")

    descriptors = {}
    print("\nVolume descriptors:")
    for lba in range(main_vds["lba"], main_vds["lba"] + (main_vds["length"] + BLOCK - 1) // BLOCK):
        block = remote.block(lba)
        tag = descriptor_tag(block)
        print(f"  LBA {lba}: tag {tag}")
        descriptors.setdefault(tag, block)
        if tag == 8:
            break

    partition = descriptors[5]
    logical = descriptors[6]
    partition_number = u16(partition, 22)
    partition_start = u32(partition, 188)
    print(f"\nPartition number={partition_number} start={partition_start} blocks={u32(partition, 192)}")
    print(f"Logical block size={u32(logical, 212)}")
    print(f"Logical volume ID={cs0(logical[84:212]).rstrip(chr(0))}")
    print(f"Domain ID={entity_id(logical, 216)}")
    fsd = long_ad(logical, 248)
    print(f"File Set Descriptor extent={fsd}")
    map_length = u32(logical, 264)
    map_count = u32(logical, 268)
    print(f"Partition maps: count={map_count} length={map_length}")
    maps = logical[440 : 440 + map_length]
    cursor = 0
    metadata_extent = None
    for index in range(map_count):
        map_type = maps[cursor]
        map_length_item = maps[cursor + 1]
        print(f"  map {index}: type={map_type} length={map_length_item} raw={maps[cursor:cursor + map_length_item].hex()}")
        if map_type == 2 and map_length_item == 64:
            metadata_extent = {
                "partition": u16(maps, cursor + 38),
                "lba": u32(maps, cursor + 40),
                "mirror_lba": u32(maps, cursor + 44),
            }
        cursor += map_length_item

    if metadata_extent:
        print(f"Metadata partition map={metadata_extent}")
        metadata_entry_block = remote.block(partition_start + metadata_extent["lba"])
        metadata_entry = file_entry(metadata_entry_block, partition_number)
        print(f"Metadata file entry={metadata_entry}")
        metadata_extent["base_lba"] = partition_start + metadata_entry["ads"][0]["lba"]
        print(f"Metadata partition base LBA={metadata_extent['base_lba']}")

    def base_lba(partition_ref):
        if partition_ref == partition_number:
            return partition_start
        if metadata_extent and partition_ref == 1:
            return metadata_extent["base_lba"]
        raise RuntimeError(f"Unknown partition reference {partition_ref}")

    fsd_block = remote.block(base_lba(fsd["partition"]) + fsd["lba"])
    print(f"\nFSD tag={descriptor_tag(fsd_block)}")
    root = long_ad(fsd_block, 400)
    print(f"Root ICB={root}")
    root_block = remote.block(base_lba(root["partition"]) + root["lba"])
    root_entry = file_entry(root_block, root["partition"])
    print(f"Root ICB entry={root_entry}")
    root_directory = read_directory(remote, base_lba, root_entry)
    print(f"Root directory bytes={len(root_directory)}")
    print("\nRoot entries:")
    root_items = directory_entries(root_directory)
    for item in root_items:
        print(f"  {item['name']!r} characteristic=0x{item['characteristic']:02x} icb={item['icb']}")

    def load_entry(item):
        icb = item["icb"]
        block = remote.block(base_lba(icb["partition"]) + icb["lba"])
        return file_entry(block, icb["partition"])

    def list_directory(item, label):
        entry = load_entry(item)
        data = read_directory(remote, base_lba, entry)
        children = directory_entries(data)
        print(f"\n{label}: {len(children)} entries")
        for child in children:
            print(f"  {child['name']!r} characteristic=0x{child['characteristic']:02x} icb={child['icb']}")
        return children

    bdmv = next((item for item in root_items if item["name"].upper() == "BDMV"), None)
    if bdmv:
        bdmv_items = list_directory(bdmv, "/BDMV")
        for directory_name in ("STREAM", "PLAYLIST", "CLIPINF"):
            directory = next((item for item in bdmv_items if item["name"].upper() == directory_name), None)
            if not directory:
                continue
            items = list_directory(directory, f"/BDMV/{directory_name}")
            for item in items[:5]:
                if directory_name == "STREAM" and item["name"].lower().endswith((".m2ts", ".mts")):
                    print(f"    {item['name']} file entry={load_entry(item)}")


if __name__ == "__main__":
    main()
