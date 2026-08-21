"""Read a remote Blu-ray ISO through HTTP Range requests.

The tool parses the UDF metadata partition in Python and exposes selected UDF
files through a local HTTP server.  ffprobe/ffmpeg can then consume a virtual
BDMV/STREAM/*.m2ts URL without downloading the whole ISO first.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import unquote, urlsplit

import requests


__version__ = "0.2.0"
BLOCK_SIZE = 2048
DEFAULT_RANGE_SIZE = 8 * 1024 * 1024
DEFAULT_WORKERS = 2
DEFAULT_PREFETCH = 2
MAX_RETRIES = 4


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def descriptor_tag(block: bytes) -> int:
    if len(block) < 16:
        return -1
    checksum = sum(block[index] for index in range(16) if index != 4) & 0xFF
    return u16(block, 0) if checksum == block[4] else -1


def decode_cs0(data: bytes) -> str:
    """Decode the OSTA CS0 compressed Unicode used by UDF names."""
    data = bytes(data).rstrip(b"\x00")
    if not data:
        return ""
    compression = data[0]
    payload = data[1:]
    if compression == 8:
        return payload.decode("latin-1", "replace").rstrip("\x00")
    if compression == 16:
        payload = payload[: len(payload) // 2 * 2]
        return payload.decode("utf-16-be", "replace").rstrip("\x00")
    return payload.decode("latin-1", "replace").rstrip("\x00")


def decode_extent_ad(data: bytes, offset: int) -> dict:
    value = u32(data, offset)
    return {
        "kind": value >> 30,
        "length": value & 0x3FFFFFFF,
        "lba": u32(data, offset + 4),
    }


def decode_long_ad(data: bytes, offset: int, implicit_partition: int | None = None) -> dict:
    value = u32(data, offset)
    return {
        "kind": value >> 30,
        "length": value & 0x3FFFFFFF,
        "lba": u32(data, offset + 4),
        "partition": u16(data, offset + 8) if implicit_partition is None else implicit_partition,
    }


def decode_file_entry(data: bytes, partition: int) -> dict:
    """Decode the UDF File Entry or Extended File Entry allocation descriptors."""
    tag = descriptor_tag(data)
    if tag not in (261, 266):
        raise RuntimeError(f"Expected UDF File Entry, got descriptor tag {tag}")
    if tag == 261:
        ea_offset, ad_base = 168, 176
    else:
        ea_offset, ad_base = 208, 216
    l_ea = u32(data, ea_offset)
    l_ad = u32(data, ea_offset + 4)
    # Allocation descriptors follow the Extended Attributes area.  Extended
    # File Entries commonly carry 24 bytes of attributes on Blu-ray/UDF
    # images, so using the fixed base offset would read attribute bytes as an
    # allocation descriptor location.
    ad_offset = ad_base + l_ea
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
        raise RuntimeError(f"Unsupported UDF ICB allocation descriptor type {ad_type}")
    for offset in range(ad_offset, ad_offset + l_ad, ad_size):
        if ad_type == 0:
            value = u32(data, offset)
            entry["ads"].append({
                "kind": value >> 30,
                "length": value & 0x3FFFFFFF,
                "lba": u32(data, offset + 4),
                "partition": partition,
            })
        elif ad_type == 1:
            entry["ads"].append(decode_long_ad(data, offset))
        else:
            value = u32(data, offset)
            entry["ads"].append({
                "kind": value >> 30,
                "length": value & 0x3FFFFFFF,
                "lba": u32(data, offset + 12),
                "partition": u16(data, offset + 16),
            })
    return entry


def decode_directory_entries(data: bytes) -> list[dict]:
    entries = []
    cursor = 0
    while cursor + 38 <= len(data):
        if descriptor_tag(data[cursor : cursor + BLOCK_SIZE]) != 257:
            break
        name_length = data[cursor + 19]
        implementation_length = u16(data, cursor + 36)
        used = 4 * ((38 + implementation_length + name_length + 3) // 4)
        if used < 40 or cursor + used > len(data):
            break
        name_start = cursor + 38 + implementation_length
        name = decode_cs0(data[name_start : name_start + name_length])
        characteristic = data[cursor + 18]
        if name and not (characteristic & 0x08):
            entries.append({
                "name": name,
                "directory": bool(characteristic & 0x02),
                "characteristic": characteristic,
                "icb": decode_long_ad(data, cursor + 20),
            })
        cursor += used
    return entries


def be16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def be32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


@dataclass(frozen=True)
class PlaylistItem:
    """One primary clip segment referenced by a Blu-ray MPLS file."""

    clip_id: str
    codec_id: str
    in_time: int
    out_time: int

    @property
    def duration_seconds(self) -> float:
        return max(0, self.out_time - self.in_time) / 45000.0


@dataclass(frozen=True)
class Playlist:
    name: str
    items: tuple[PlaylistItem, ...]
    size_bytes: int = 0
    unique_size_bytes: int = 0
    audio_stream_count: int = -1

    @property
    def duration_seconds(self) -> float:
        return sum(item.duration_seconds for item in self.items)

    @property
    def clip_ids(self) -> tuple[str, ...]:
        return tuple(item.clip_id for item in self.items)

    @property
    def total_bitrate_mbps(self) -> float:
        if self.duration_seconds <= 0:
            return 0.0
        return self.size_bytes * 8 / self.duration_seconds / 1_000_000


def parse_mpls(data: bytes, name: str = "") -> Playlist:
    """Parse the primary play items from a Blu-ray MPLS playlist.

    The fields used here follow libbluray's mpls_parse implementation.  The
    parser intentionally focuses on the primary timeline: secondary paths,
    playlist marks, and stream metadata are not required to build an ffmpeg
    concat input.
    """
    if len(data) < 20 or data[:4] != b"MPLS":
        raise RuntimeError(f"Invalid MPLS header: {name or '<unnamed>'}")

    list_pos = be32(data, 8)
    if list_pos + 10 > len(data):
        raise RuntimeError(f"MPLS playlist section is outside the file: {name}")

    playlist_length = be32(data, list_pos)
    playlist_end = list_pos + 4 + playlist_length
    if playlist_end > len(data):
        raise RuntimeError(f"MPLS playlist section is truncated: {name}")

    item_count = be16(data, list_pos + 6)
    cursor = list_pos + 10
    items: list[PlaylistItem] = []
    audio_counts: list[int] = []
    for index in range(item_count):
        if cursor + 2 > playlist_end:
            raise RuntimeError(f"MPLS play item {index} is truncated: {name}")
        item_length = be16(data, cursor)
        item_start = cursor + 2
        item_end = item_start + item_length
        if item_length < 20 or item_end > playlist_end or item_end > len(data):
            raise RuntimeError(f"Invalid MPLS play item {index}: {name}")

        clip_id = data[item_start : item_start + 5].decode("ascii", "replace")
        codec_id = data[item_start + 5 : item_start + 9].decode("ascii", "replace")
        in_time = be32(data, item_start + 12)
        out_time = be32(data, item_start + 16)
        if out_time < in_time:
            raise RuntimeError(f"MPLS play item {index} has a negative duration: {name}")
        items.append(PlaylistItem(clip_id, codec_id, in_time, out_time))

        # The STN block starts after the fixed play-item fields and any
        # additional angle clip descriptors.  Its fifth byte is the primary
        # audio stream count.  It lets audio batch extraction skip video-only
        # feature clips without probing each remote M2TS first.
        stn_start = item_start + 32
        packed_flags = be16(data, item_start + 9)
        is_multi_angle = bool(packed_flags & 0x0010)
        if is_multi_angle:
            angle_count = max(1, data[item_start + 32])
            stn_start = item_start + 34 + (angle_count - 1) * 10
        if stn_start + 6 <= item_end:
            audio_counts.append(data[stn_start + 5])
        cursor = item_end

    audio_stream_count = max(audio_counts) if audio_counts else -1
    return Playlist(name=name, items=tuple(items), audio_stream_count=audio_stream_count)


def source_to_url(source: str | Path) -> str:
    """Read a .strm file or accept an HTTP(S) ISO URL directly."""
    value = str(source).strip()
    if value.lower().startswith(("http://", "https://")):
        return value

    source_path = Path(value)
    if not source_path.is_file():
        raise FileNotFoundError(
            f"Source is neither an HTTP(S) URL nor an existing .strm file: {value}"
        )
    url = source_path.read_text(encoding="utf-8-sig").strip()
    if not url:
        raise RuntimeError(f"Empty STRM file: {source_path}")
    if not url.lower().startswith(("http://", "https://")):
        raise RuntimeError(f"STRM does not contain an HTTP(S) URL: {source_path}")
    return url


class RemoteRangeReader:
    def __init__(
        self,
        url: str,
        verbose: bool = False,
        workers: int = DEFAULT_WORKERS,
        prefetch: int = DEFAULT_PREFETCH,
        range_size: int = DEFAULT_RANGE_SIZE,
    ):
        if workers < 1:
            raise ValueError("workers must be at least 1")
        if prefetch < 0:
            raise ValueError("prefetch must be non-negative")
        if range_size < BLOCK_SIZE or range_size % BLOCK_SIZE:
            raise ValueError(f"range_size must be a multiple of {BLOCK_SIZE} bytes")

        self.verbose = verbose
        self.workers = workers
        self.prefetch = prefetch
        self.range_size = range_size
        self.lock = threading.RLock()
        self._thread_local = threading.local()
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._inflight = {}
        self._executor = None

        session = requests.Session()
        response = session.get(
            url,
            headers={"Range": "bytes=0-0"},
            allow_redirects=True,
            timeout=30,
        )
        try:
            response.raise_for_status()
            if response.status_code != 206:
                raise RuntimeError(f"Remote server does not support Range: HTTP {response.status_code}")
            content_range = response.headers.get("Content-Range", "")
            if "/" not in content_range:
                raise RuntimeError("Remote response has no Content-Range header")
            self.size = int(content_range.rsplit("/", 1)[1])
            self.final_url = response.url
        finally:
            response.close()
        self._base_cookies = session.cookies.copy()
        session.close()

        # Keep the default memory footprint bounded even when range-size is
        # increased.  The current chunk plus the prefetch window remain
        # available whenever the configured size fits within this cap.
        cache_memory_cap = 256 * 1024 * 1024
        memory_limit = max(1, cache_memory_cap // self.range_size)
        self.cache_limit = max(1, min(workers + prefetch + 2, memory_limit))
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix="remote-bluray-range",
        )
        if verbose:
            print(f"ISO size: {self.size:,} bytes")
            print(f"Final URL: {self.final_url[:120]}...")
            print(
                f"Range reader: workers={self.workers}, "
                f"prefetch={self.prefetch}, range-size={self.range_size:,} bytes"
            )

    def _session_for_thread(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.cookies.update(self._base_cookies)
            self._thread_local.session = session
        return session

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            return min(8.0, 0.5 * (2**attempt))

    def _fetch(self, start: int, end: int) -> bytes:
        expected = end - start + 1
        session = self._session_for_thread()
        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            response = None
            try:
                response = session.get(
                    self.final_url,
                    headers={"Range": f"bytes={start}-{end}"},
                    timeout=60,
                )
                if response.status_code == 206:
                    data = response.content
                    if len(data) != expected:
                        raise IOError(
                            f"Range returned {len(data):,} bytes; expected {expected:,}"
                        )
                    if self.verbose:
                        print(f"Remote Range: {start}-{end} -> {len(data):,} bytes")
                    return data

                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable:
                    raise RuntimeError(f"Range request failed: HTTP {response.status_code}")
                last_error = IOError(f"Range request failed: HTTP {response.status_code}")
                if attempt >= MAX_RETRIES:
                    raise last_error
                delay = self._retry_delay(response, attempt)
                if self.verbose:
                    print(
                        f"Range {start}-{end} got HTTP {response.status_code}; "
                        f"retrying in {delay:.1f}s"
                    )
                time.sleep(delay)
            except (requests.RequestException, IOError) as error:
                last_error = error
                if attempt >= MAX_RETRIES:
                    raise IOError(f"Range request failed for {start}-{end}: {error}") from error
                delay = min(8.0, 0.5 * (2**attempt))
                if self.verbose:
                    print(f"Range {start}-{end} failed; retrying in {delay:.1f}s: {error}")
                time.sleep(delay)
            finally:
                if response is not None:
                    response.close()
        raise IOError(f"Range request failed for {start}-{end}: {last_error}")

    def _fetch_chunk(self, index: int) -> bytes:
        start = index * self.range_size
        if start >= self.size:
            return b""
        end = min(start + self.range_size, self.size) - 1
        return self._fetch(start, end)

    def _complete_chunk(self, index: int, future) -> None:
        try:
            data = future.result()
        except Exception:
            with self.lock:
                if self._inflight.get(index) is future:
                    self._inflight.pop(index, None)
            return
        with self.lock:
            if self._inflight.get(index) is future:
                self._inflight.pop(index, None)
            if data:
                self._cache[index] = data
                self._cache.move_to_end(index)
                while len(self._cache) > self.cache_limit:
                    self._cache.popitem(last=False)

    def _ensure_chunk(self, index: int) -> None:
        if index * self.range_size >= self.size:
            return
        with self.lock:
            if index in self._cache or index in self._inflight:
                return
            future = self._executor.submit(self._fetch_chunk, index)
            self._inflight[index] = future
            future.add_done_callback(lambda completed: self._complete_chunk(index, completed))

    def _get_chunk(self, index: int) -> bytes:
        with self.lock:
            data = self._cache.get(index)
            if data is not None:
                self._cache.move_to_end(index)
                return data
            future = self._inflight.get(index)
            if future is None:
                future = self._executor.submit(self._fetch_chunk, index)
                self._inflight[index] = future
                future.add_done_callback(lambda completed: self._complete_chunk(index, completed))
        return future.result()

    def _schedule_prefetch(self, index: int) -> None:
        for next_index in range(index + 1, index + 1 + self.prefetch):
            self._ensure_chunk(next_index)

    def read_range(self, start: int, size: int) -> bytes:
        if size <= 0 or start >= self.size:
            return b""
        start = max(0, start)
        end = min(start + size, self.size)
        output = bytearray()
        while start < end:
            index = start // self.range_size
            data = self._get_chunk(index)
            if not data:
                break
            chunk_start = index * self.range_size
            offset = start - chunk_start
            count = min(end - start, len(data) - offset)
            if count <= 0:
                raise IOError(f"Invalid cached Range chunk at index {index}")
            output.extend(data[offset : offset + count])
            start += count
            self._schedule_prefetch(index)
        return bytes(output)

    def read_block(self, lba: int) -> bytes:
        data = self.read_range(lba * BLOCK_SIZE, BLOCK_SIZE)
        if len(data) != BLOCK_SIZE:
            raise IOError(f"Short read at LBA {lba}: {len(data)} bytes")
        return data


@dataclass
class UdfFile:
    image: "RemoteUdfImage"
    path: str
    entry: dict
    partition: int

    @property
    def size(self) -> int:
        return self.entry["length"]

    def _locate(self, offset: int) -> tuple[dict, int]:
        if offset < 0 or offset >= self.size:
            raise ValueError(f"Offset outside {self.path}: {offset}")
        remaining = offset
        for ad in self.entry["ads"]:
            if remaining < ad["length"]:
                return ad, remaining
            remaining -= ad["length"]
        raise IOError(f"No allocation descriptor covers offset {offset} in {self.path}")

    def read_at(self, offset: int, size: int) -> bytes:
        if offset >= self.size or size <= 0:
            return b""
        size = min(size, self.size - offset)
        output = bytearray()
        while size:
            ad, within = self._locate(offset)
            available = min(size, ad["length"] - within)
            if ad["kind"] == 0:
                absolute = self.image.partition_base(ad["partition"]) + ad["lba"] * BLOCK_SIZE + within
                output.extend(self.image.remote.read_range(absolute, available))
            elif ad["kind"] in (1, 2):
                output.extend(b"\x00" * available)
            else:
                raise IOError(f"Unsupported allocation extent type {ad['kind']} in {self.path}")
            offset += available
            size -= available
        return bytes(output)

    def read_all(self) -> bytes:
        output = bytearray()
        offset = 0
        while offset < self.size:
            chunk = self.read_at(
                offset,
                min(self.image.remote.range_size, self.size - offset),
            )
            if not chunk:
                break
            output.extend(chunk)
            offset += len(chunk)
        return bytes(output)


class RemoteUdfImage:
    def __init__(
        self,
        source: str | Path,
        verbose: bool = False,
        workers: int = DEFAULT_WORKERS,
        prefetch: int = DEFAULT_PREFETCH,
        range_size: int = DEFAULT_RANGE_SIZE,
    ):
        self.source = str(source)
        self.url = source_to_url(source)
        self.verbose = verbose
        self.remote = RemoteRangeReader(
            self.url,
            verbose=verbose,
            workers=workers,
            prefetch=prefetch,
            range_size=range_size,
        )
        self.partition_starts: dict[int, int] = {}
        self.partition_lengths: dict[int, int] = {}
        self.metadata_partition = None
        self.root_icb = None
        self._parse_volume()

    def partition_base(self, partition: int) -> int:
        if partition not in self.partition_starts:
            raise RuntimeError(f"Unknown UDF partition reference: {partition}")
        return self.partition_starts[partition] * BLOCK_SIZE

    def _parse_volume(self):
        total_blocks = self.remote.size // BLOCK_SIZE
        anchor = None
        for lba in (256, total_blocks - 1, total_blocks - 257):
            block = self.remote.read_block(lba)
            if descriptor_tag(block) == 2:
                anchor = block
                break
        if anchor is None:
            raise RuntimeError("No UDF Anchor Volume Descriptor Pointer found")

        main_vds = decode_extent_ad(anchor, 16)
        descriptors = {}
        for lba in range(main_vds["lba"], main_vds["lba"] + (main_vds["length"] + BLOCK_SIZE - 1) // BLOCK_SIZE):
            block = self.remote.read_block(lba)
            tag = descriptor_tag(block)
            descriptors.setdefault(tag, block)
            if tag == 8:
                break
        if 5 not in descriptors or 6 not in descriptors:
            raise RuntimeError("UDF volume descriptor sequence is incomplete")

        partition_descriptor = descriptors[5]
        logical_volume = descriptors[6]
        physical_partition = u16(partition_descriptor, 22)
        physical_start = u32(partition_descriptor, 188)
        physical_length = u32(partition_descriptor, 192)
        self.partition_starts[physical_partition] = physical_start
        self.partition_lengths[physical_partition] = physical_length
        self.volume_id = decode_cs0(logical_volume[84:212])

        map_length = u32(logical_volume, 264)
        map_count = u32(logical_volume, 268)
        maps = logical_volume[440 : 440 + map_length]
        cursor = 0
        metadata_map = None
        for _ in range(map_count):
            if cursor + 2 > len(maps):
                break
            map_type = maps[cursor]
            item_length = maps[cursor + 1]
            if item_length < 2 or cursor + item_length > len(maps):
                raise RuntimeError("Invalid UDF partition map")
            if map_type == 1:
                logical_partition = cursor // 1
                self.partition_starts[logical_partition] = physical_start
                self.partition_lengths[logical_partition] = physical_length
            elif map_type == 2 and item_length == 64:
                metadata_map = {
                    "partition": u16(maps, cursor + 38),
                    "lba": u32(maps, cursor + 40),
                    "mirror_lba": u32(maps, cursor + 44),
                    "index": len(self.partition_starts),
                }
            cursor += item_length

        # The logical partition number is the partition-map index.  Blu-ray
        # metadata maps are normally map 0=physical and map 1=metadata.
        self.partition_starts[0] = physical_start
        self.partition_lengths[0] = physical_length
        if metadata_map:
            metadata_entry_block = self.remote.read_block(physical_start + metadata_map["lba"])
            metadata_entry = decode_file_entry(metadata_entry_block, physical_partition)
            if not metadata_entry["ads"]:
                raise RuntimeError("UDF metadata file has no allocation descriptor")
            metadata_base = physical_start + metadata_entry["ads"][0]["lba"]
            metadata_partition = 1
            self.partition_starts[metadata_partition] = metadata_base
            self.partition_lengths[metadata_partition] = metadata_entry["length"] // BLOCK_SIZE
            self.metadata_partition = metadata_partition

        fsd_location = decode_long_ad(logical_volume, 248)
        fsd = self._read_descriptor(fsd_location)
        if descriptor_tag(fsd) != 256:
            raise RuntimeError(f"UDF File Set Descriptor not found, got tag {descriptor_tag(fsd)}")
        self.root_icb = decode_long_ad(fsd, 400)
        if self.verbose:
            print(f"UDF volume: {self.volume_id}")
            print(f"UDF partitions: {self.partition_starts}")
            print(f"Root ICB: {self.root_icb}")

    def _read_descriptor(self, extent: dict) -> bytes:
        absolute = self.partition_base(extent["partition"]) + extent["lba"] * BLOCK_SIZE
        return self.remote.read_range(absolute, BLOCK_SIZE)

    def _entry_from_icb(self, icb: dict) -> dict:
        block = self.remote.read_block(self.partition_starts[icb["partition"]] + icb["lba"])
        return decode_file_entry(block, icb["partition"])

    def _directory_from_entry(self, entry: dict, partition: int) -> list[dict]:
        udf_file = UdfFile(self, "<directory>", entry, partition)
        return decode_directory_entries(udf_file.read_all())

    def _find_in_directory(self, entries: Iterable[dict], name: str) -> dict:
        folded = name.casefold()
        for entry in entries:
            if entry["name"].casefold() == folded:
                return entry
        raise FileNotFoundError(name)

    def find(self, path: str) -> UdfFile:
        components = [part for part in path.replace("\\", "/").split("/") if part]
        current_entry = {"directory": True, "icb": self.root_icb}
        current_partition = self.root_icb["partition"]
        current_path = ""
        for component in components:
            directory_entry = self._entry_from_icb(current_entry["icb"])
            if directory_entry["file_type"] != 4:
                raise NotADirectoryError(current_path or "/")
            entries = self._directory_from_entry(directory_entry, current_partition)
            current_entry = self._find_in_directory(entries, component)
            current_partition = current_entry["icb"]["partition"]
            current_path = f"{current_path}/{current_entry['name']}"
        entry = self._entry_from_icb(current_entry["icb"])
        return UdfFile(self, path or "/", entry, current_partition)

    def list_dir(self, path: str) -> list[dict]:
        directory = self.find(path)
        if directory.entry["file_type"] != 4:
            raise NotADirectoryError(path)
        return self._directory_from_entry(directory.entry, directory.partition)

    def stream_candidates(self) -> list[tuple[str, UdfFile]]:
        candidates = []
        for item in self.list_dir("/BDMV/STREAM"):
            if item["directory"] or not item["name"].lower().endswith((".m2ts", ".mts")):
                continue
            path = f"/BDMV/STREAM/{item['name']}"
            candidates.append((item["name"], self.find(path)))
        return sorted(candidates, key=lambda value: value[1].size, reverse=True)

    def playlist_candidates(self) -> list[Playlist]:
        playlists = []
        for item in self.list_dir("/BDMV/PLAYLIST"):
            name = item["name"]
            if item["directory"] or not name.lower().endswith(".mpls"):
                continue
            path = f"/BDMV/PLAYLIST/{name}"
            playlist = parse_mpls(self.find(path).read_all(), name=name)
            clip_sizes = {
                item.clip_id: self.find(f"/BDMV/STREAM/{item.clip_id}.m2ts").size
                for item in playlist.items
            }
            size_bytes = sum(clip_sizes.get(item.clip_id, 0) for item in playlist.items)
            playlists.append(
                Playlist(
                    name=playlist.name,
                    items=playlist.items,
                    size_bytes=size_bytes,
                    unique_size_bytes=sum(clip_sizes.values()),
                    audio_stream_count=playlist.audio_stream_count,
                )
            )
        return sorted(playlists, key=lambda playlist: playlist.name.casefold())


class VirtualFileServer:
    def __init__(self, image: RemoteUdfImage, verbose: bool = False):
        self.image = image
        self.verbose = verbose
        self.server = None

    def __enter__(self):
        image = self.image
        verbose = self.verbose

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format_string, *args):
                if verbose:
                    print(f"[local-http] {self.address_string()} {format_string % args}")

            def _file(self):
                path = unquote(urlsplit(self.path).path)
                if not path.startswith("/"):
                    path = "/" + path
                return image.find(path)

            def _send(self, include_body: bool):
                try:
                    remote_file = self._file()
                except (FileNotFoundError, NotADirectoryError):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                except Exception as error:
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
                    return

                start = 0
                end = remote_file.size - 1
                range_header = self.headers.get("Range")
                if range_header:
                    if not range_header.startswith("bytes=") or "," in range_header:
                        self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        return
                    spec = range_header[6:]
                    start_text, separator, end_text = spec.partition("-")
                    try:
                        if start_text:
                            start = int(start_text)
                            end = int(end_text) if end_text else remote_file.size - 1
                        else:
                            length = int(end_text)
                            start = max(0, remote_file.size - length)
                        if start < 0 or start >= remote_file.size or end < start:
                            raise ValueError
                        end = min(end, remote_file.size - 1)
                    except ValueError:
                        self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                        return

                length = end - start + 1
                status = HTTPStatus.PARTIAL_CONTENT if range_header else HTTPStatus.OK
                self.send_response(status)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if range_header:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{remote_file.size}")
                self.send_header("Connection", "close")
                self.end_headers()
                if not include_body:
                    return
                position = start
                while position <= end:
                    chunk_size = min(1024 * 1024, end - position + 1)
                    chunk = remote_file.read_at(position, chunk_size)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                        return
                    position += len(chunk)

            def do_HEAD(self):
                self._send(False)

            def do_GET(self):
                self._send(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    @property
    def url(self):
        if self.server is None:
            raise RuntimeError("Virtual server is not running")
        return f"http://127.0.0.1:{self.server.server_port}"

    def file_url(self, path: str) -> str:
        return self.url + "/" + path.lstrip("/")

    def __exit__(self, exc_type, exc, traceback):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)


def get_executable(name: str) -> str:
    environment_name = {
        "ffmpeg": "REMOTE_BLURAY_FFMPEG",
        "ffprobe": "REMOTE_BLURAY_FFPROBE",
    }.get(name)
    if environment_name:
        configured = os.environ.get(environment_name)
        if configured and Path(configured).is_file():
            return configured

    path = shutil.which(name)
    if path:
        return path
    candidates = {
        "ffmpeg": r"C:\ffmpeg\bin\ffmpeg.exe",
        "ffprobe": r"C:\ffmpeg\bin\ffprobe.exe",
    }
    candidate = candidates.get(name)
    if candidate and Path(candidate).exists():
        return candidate
    raise FileNotFoundError(f"Executable not found: {name}")


def choose_stream(image: RemoteUdfImage, requested: str | None) -> tuple[str, UdfFile]:
    candidates = image.stream_candidates()
    if not candidates:
        raise RuntimeError("No M2TS files found in /BDMV/STREAM")
    if requested:
        requested = requested.lower()
        for name, file in candidates:
            if name.lower() == requested:
                return name, file
        raise FileNotFoundError(f"M2TS not found: {requested}")
    return candidates[0]


def normalize_playlist_name(requested: str) -> str:
    value = requested.strip()
    if value.isdigit():
        return f"{int(value):05d}.mpls"
    if not value.lower().endswith(".mpls"):
        value += ".mpls"
    return value


def choose_playlist(image: RemoteUdfImage, requested: str | None) -> Playlist:
    playlists = image.playlist_candidates()
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")
    if requested:
        wanted = normalize_playlist_name(requested).casefold()
        for playlist in playlists:
            if playlist.name.casefold() == wanted:
                return playlist
        raise FileNotFoundError(f"MPLS playlist not found: {requested}")
    return max(playlists, key=lambda playlist: playlist.duration_seconds)


def format_duration(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours}:{minutes:02d}:{seconds_int:02d}.{milliseconds:03d}"


def parse_duration(value: str) -> float:
    """Parse seconds or H:M:S(.ms) for the feature-list threshold."""
    text = str(value).strip()
    if not text:
        raise ValueError("Duration cannot be empty")
    if ":" not in text:
        seconds = float(text)
    else:
        parts = text.split(":")
        if len(parts) not in (2, 3):
            raise ValueError(f"Invalid duration: {value}")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as error:
            raise ValueError(f"Invalid duration: {value}") from error
        if len(numbers) == 2:
            minutes, seconds_part = numbers
            hours = 0.0
        else:
            hours, minutes, seconds_part = numbers
        if minutes < 0 or seconds_part < 0 or seconds_part >= 60 or minutes >= 60:
            raise ValueError(f"Invalid duration: {value}")
        seconds = hours * 3600 + minutes * 60 + seconds_part
    if seconds < 0:
        raise ValueError(f"Duration must be non-negative: {value}")
    return seconds


def ffmpeg_seconds(timestamp: int) -> str:
    return f"{timestamp / 45000.0:.6f}"


def main_playlist(playlists: list[Playlist]) -> Playlist:
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")
    # Count each referenced M2TS once for main detection.  Some discs contain
    # fake/looping playlists that repeat one short clip hundreds of times;
    # counting every repetition would incorrectly make those playlists the
    # largest title.
    return max(
        playlists,
        key=lambda playlist: (
            playlist.unique_size_bytes,
            playlist.size_bytes,
            playlist.duration_seconds,
        ),
    )


def select_playlists(
    image: RemoteUdfImage,
    requested: list[str] | None,
    mode: str | None,
    min_duration: str | None,
) -> list[Playlist]:
    """Resolve explicit playlist names or the main/feature automatic modes."""
    playlists = image.playlist_candidates()
    if not playlists:
        raise RuntimeError("No valid .mpls playlists found in /BDMV/PLAYLIST")

    if requested:
        by_name = {playlist.name.casefold(): playlist for playlist in playlists}
        selected = []
        seen = set()
        for value in requested:
            name = normalize_playlist_name(value).casefold()
            if name not in by_name:
                raise FileNotFoundError(f"MPLS playlist not found: {value}")
            if name not in seen:
                selected.append(by_name[name])
                seen.add(name)
        return selected

    if mode == "main":
        return [main_playlist(playlists)]

    if mode == "feat":
        if min_duration is None:
            raise ValueError("feat mode requires --min-duration")
        threshold = parse_duration(min_duration)
        main = main_playlist(playlists)
        selected = [
            playlist
            for playlist in playlists
            if playlist.name.casefold() != main.name.casefold()
            and playlist.duration_seconds >= threshold
            and playlist.duration_seconds <= main.duration_seconds
        ]
        return sorted(selected, key=lambda playlist: (-playlist.size_bytes, playlist.name.casefold()))

    raise ValueError("Specify --playlist, --mode main, or --mode feat")


def playlist_summary(playlist: Playlist) -> str:
    display_name = playlist.name.rsplit(".", 1)[0] + ".MPLS"
    return (
        f"Name:                   {display_name}\n"
        f"Length:                 {format_duration(playlist.duration_seconds)} (h:m:s.ms)\n"
        f"Size:                   {playlist.size_bytes:,} bytes\n"
        f"Total Bitrate:          {playlist.total_bitrate_mbps:.2f} Mbps"
    )


@contextmanager
def virtual_input(
    image: RemoteUdfImage,
    server: VirtualFileServer,
    stream_name: str | None,
    playlist_name: str | None,
) -> Iterator[tuple[list[str], str]]:
    """Yield ffmpeg input arguments for either one M2TS or one MPLS timeline."""
    if playlist_name:
        playlist = choose_playlist(image, playlist_name)
        if not playlist.items:
            raise RuntimeError(f"Playlist has no primary play items: {playlist.name}")

        with tempfile.TemporaryDirectory(prefix="remote_bluray_") as temp_dir:
            concat_path = Path(temp_dir) / f"{Path(playlist.name).stem}.ffconcat"
            lines = ["ffconcat version 1.0"]
            for item in playlist.items:
                stream_path = f"/BDMV/STREAM/{item.clip_id}.m2ts"
                clip = image.find(stream_path)
                # The virtual URL is local and contains only ASCII path parts.
                lines.append(f"file '{server.file_url(stream_path)}'")
                if item.in_time:
                    lines.append(f"inpoint {ffmpeg_seconds(item.in_time)}")
                if item.out_time:
                    lines.append(f"outpoint {ffmpeg_seconds(item.out_time)}")
                if clip.size <= 0:
                    raise RuntimeError(f"Empty M2TS clip referenced by {playlist.name}: {item.clip_id}")
            concat_path.write_text("\n".join(lines) + "\n", encoding="ascii")
            label = (
                f"Playlist: {playlist.name} "
                f"({len(playlist.items)} play item(s), {format_duration(playlist.duration_seconds)})"
            )
            yield (
                [
                    "-protocol_whitelist",
                    "file,http,https,tcp,tls",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_path),
                ],
                label,
            )
        return

    name, file = choose_stream(image, stream_name)
    path = f"/BDMV/STREAM/{name}"
    yield ["-i", server.file_url(path)], f"Stream: {name} ({file.size:,} bytes)"


def output_paths(output_name: str, playlists: list[Playlist], kind: str) -> list[Path]:
    """Build non-overlapping output paths for one or many playlist jobs."""
    output = Path(output_name)
    default_suffix = ".mka" if kind == "audio" else ".mkv"
    known_suffixes = {".mka", ".mkv", ".m2ts", ".ts", ".mp4"}
    if output.suffix.casefold() in known_suffixes:
        if len(playlists) == 1:
            return [output]
        directory = output.parent
        prefix = output.stem + "-"
        suffix = output.suffix
    else:
        directory = output
        prefix = ""
        suffix = default_suffix
    return [
        directory / f"{prefix}{Path(playlist.name).stem}{suffix}"
        for playlist in playlists
    ]


def input_has_audio(input_args: list[str]) -> bool:
    return bool(input_audio_codecs(input_args))


def input_audio_codecs(input_args: list[str]) -> set[str]:
    command = [
        get_executable("ffprobe"),
        "-hide_banner",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
    ] + input_args
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed while checking audio codecs")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise RuntimeError("ffprobe returned invalid JSON while checking audio codecs") from error
    return {
        str(stream.get("codec_name", "")).casefold()
        for stream in payload.get("streams", [])
        if stream.get("codec_name")
    }


def image_from_args(args) -> RemoteUdfImage:
    return RemoteUdfImage(
        args.source,
        verbose=args.verbose,
        workers=args.workers,
        prefetch=args.prefetch,
        range_size=args.range_size,
    )


def command_list(args):
    image = image_from_args(args)
    playlists = image.playlist_candidates()
    print(f"Playlist Count:          {len(playlists)}")
    print("（不计入 .mpls.backup）\n")
    for playlist in playlists:
        print(playlist_summary(playlist))
        print()


def command_probe(args):
    image = image_from_args(args)
    with VirtualFileServer(image, verbose=args.verbose) as server:
        with virtual_input(image, server, args.stream, args.playlist) as selected:
            input_args, label = selected
            command = [
                get_executable("ffprobe"),
                "-hide_banner",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
            ] + input_args
            print(f"\n{label}")
            print("Running:", " ".join(command))
            result = subprocess.run(command, check=False)
            if result.returncode:
                raise SystemExit(result.returncode)


def command_extract_audio(args):
    image = image_from_args(args)
    if args.playlist or args.mode:
        playlists = select_playlists(image, args.playlist, args.mode, args.min_duration)
        if not playlists:
            raise RuntimeError("No feature playlists meet the --min-duration threshold")
        outputs = output_paths(args.output, playlists, "audio")
    else:
        playlists = []
        outputs = [Path(args.output)]

    with VirtualFileServer(image, verbose=args.verbose) as server:
        if playlists:
            jobs = zip(playlists, outputs)
        else:
            jobs = [(None, outputs[0])]
        for playlist, output in jobs:
            playlist_name = playlist.name if playlist else None
            with virtual_input(image, server, args.stream, playlist_name) as selected:
                input_args, label = selected
                has_audio = playlist is None or playlist.audio_stream_count != 0
                if playlist and playlist.audio_stream_count < 0:
                    has_audio = input_has_audio(input_args)
                if playlist and not has_audio:
                    print(f"\nSkip: {playlist.name} does not contain an audio stream")
                    continue
                audio_codecs = input_audio_codecs(input_args)
                audio_codec = "pcm_s24le" if "pcm_bluray" in audio_codecs else "copy"
                if audio_codec == "pcm_s24le":
                    print("Audio: pcm_bluray -> pcm_s24le (lossless Matroska-compatible conversion)")
                output.parent.mkdir(parents=True, exist_ok=True)
                command = [
                    get_executable("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                ] + input_args + [
                    "-map",
                    args.map,
                    "-c:a",
                    audio_codec,
                ]
                if args.duration:
                    command.extend(["-t", args.duration])
                command.extend(["-y", str(output)])
                print(f"\n{label}")
                print("Running:", " ".join(command))
                result = subprocess.run(command, check=False)
                if result.returncode:
                    raise SystemExit(result.returncode)
                print(f"Output: {output}")


def command_extract_video(args):
    image = image_from_args(args)
    if args.playlist or args.mode:
        playlists = select_playlists(image, args.playlist, args.mode, args.min_duration)
        if not playlists:
            raise RuntimeError("No feature playlists meet the --min-duration threshold")
        outputs = output_paths(args.output, playlists, "video")
    else:
        playlists = []
        outputs = [Path(args.output)]

    with VirtualFileServer(image, verbose=args.verbose) as server:
        if playlists:
            jobs = zip(playlists, outputs)
        else:
            jobs = [(None, outputs[0])]
        for playlist, output in jobs:
            output.parent.mkdir(parents=True, exist_ok=True)
            playlist_name = playlist.name if playlist else None
            with virtual_input(image, server, args.stream, playlist_name) as selected:
                input_args, label = selected
                audio_codecs = input_audio_codecs(input_args)
                if "pcm_bluray" in audio_codecs:
                    codec_args = ["-c", "copy", "-c:a", "pcm_s24le"]
                else:
                    codec_args = ["-c", "copy"]
                if "pcm_bluray" in audio_codecs:
                    print("Audio: pcm_bluray -> pcm_s24le (lossless Matroska-compatible conversion)")
                command = [
                    get_executable("ffmpeg"),
                    "-hide_banner",
                    "-nostdin",
                ] + input_args + [
                    "-map",
                    args.map,
                ] + codec_args
                if args.duration:
                    command.extend(["-t", args.duration])
                command.extend(["-y", str(output)])
                print(f"\n{label}")
                print("Running:", " ".join(command))
                result = subprocess.run(command, check=False)
                if result.returncode:
                    raise SystemExit(result.returncode)
                print(f"Output: {output}")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_range_size(value: str) -> int:
    text = str(value).strip().lower()
    units = {
        "k": 1024,
        "kb": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gib": 1024**3,
    }
    multiplier = 1
    number = text
    for suffix in sorted(units, key=len, reverse=True):
        if text.endswith(suffix):
            number = text[: -len(suffix)].strip()
            multiplier = units[suffix]
            break
    try:
        parsed = int(float(number) * multiplier)
    except ValueError as error:
        raise argparse.ArgumentTypeError("use a byte count such as 8388608 or 8M") from error
    if parsed < BLOCK_SIZE or parsed % BLOCK_SIZE:
        raise argparse.ArgumentTypeError(
            f"must be at least {BLOCK_SIZE} bytes and a multiple of {BLOCK_SIZE}"
        )
    return parsed


def add_remote_options(parser, suppress_defaults: bool = False) -> None:
    default_workers = argparse.SUPPRESS if suppress_defaults else DEFAULT_WORKERS
    default_prefetch = argparse.SUPPRESS if suppress_defaults else DEFAULT_PREFETCH
    default_range_size = argparse.SUPPRESS if suppress_defaults else DEFAULT_RANGE_SIZE
    parser.add_argument(
        "--workers",
        type=positive_int,
        default=default_workers,
        help=f"parallel Range downloads (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--prefetch",
        type=nonnegative_int,
        default=default_prefetch,
        help=f"number of future Range chunks to prefetch (default: {DEFAULT_PREFETCH})",
    )
    parser.add_argument(
        "--range-size",
        type=parse_range_size,
        default=default_range_size,
        metavar="SIZE",
        help="HTTP Range chunk size, e.g. 4M, 8M, or 8388608",
    )


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"remote-bluray {__version__}")
    parser.add_argument("--verbose", action="store_true", help="show local HTTP and remote Range activity")
    add_remote_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list Blu-ray streams and playlists")
    add_remote_options(list_parser, suppress_defaults=True)
    list_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    list_parser.set_defaults(func=command_list)

    probe_parser = subparsers.add_parser("probe", help="probe a virtual M2TS or MPLS timeline with ffprobe")
    add_remote_options(probe_parser, suppress_defaults=True)
    probe_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    probe_selection = probe_parser.add_mutually_exclusive_group()
    probe_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    probe_selection.add_argument("--playlist", help="MPLS filename/number, e.g. 00000.mpls or 0")
    probe_parser.set_defaults(func=command_probe)

    extract_parser = subparsers.add_parser("extract-audio", help="copy one audio stream to an MKA file")
    add_remote_options(extract_parser, suppress_defaults=True)
    extract_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    extract_parser.add_argument("-o", "--output", required=True, help="output audio filename or directory; directories receive playlist-based .mka names")
    extract_selection = extract_parser.add_mutually_exclusive_group()
    extract_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    extract_selection.add_argument(
        "--playlist",
        nargs="+",
        help="one or more MPLS filenames/numbers, e.g. 00000.mpls 00001.mpls",
    )
    extract_selection.add_argument("--mode", choices=("main", "feat"), help="automatic playlist selection mode")
    extract_parser.add_argument(
        "--min-duration",
        help="feat mode minimum playlist duration, in seconds or H:M:S(.ms)",
    )
    extract_parser.add_argument("--map", default="0:a:0", help="ffmpeg stream map, e.g. 0:a:0")
    extract_parser.add_argument("--duration", help="optional short duration for a smoke test, e.g. 1")
    extract_parser.set_defaults(func=command_extract_audio)

    video_parser = subparsers.add_parser(
        "extract-video",
        help="copy the complete selected M2TS or MPLS timeline (video and associated streams)",
    )
    add_remote_options(video_parser, suppress_defaults=True)
    video_parser.add_argument("source", help=".strm 文件路径或 HTTP(S) ISO URL")
    video_parser.add_argument("-o", "--output", required=True, help="output video filename or directory; directories receive playlist-based .mkv names")
    video_selection = video_parser.add_mutually_exclusive_group()
    video_selection.add_argument("--stream", help="M2TS filename; defaults to the largest stream")
    video_selection.add_argument(
        "--playlist",
        nargs="+",
        help="one or more MPLS filenames/numbers, e.g. 00000.mpls 00001.mpls",
    )
    video_selection.add_argument("--mode", choices=("main", "feat"), help="automatic playlist selection mode")
    video_parser.add_argument(
        "--min-duration",
        help="feat mode minimum playlist duration, in seconds or H:M:S(.ms)",
    )
    video_parser.add_argument("--map", default="0", help="ffmpeg stream map; defaults to all streams")
    video_parser.add_argument("--duration", help="optional short duration for a smoke test, e.g. 1")
    video_parser.set_defaults(func=command_extract_video)
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.verbose = args.verbose or getattr(args, "verbose", False)
    args.func(args)


if __name__ == "__main__":
    main()
