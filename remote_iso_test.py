import io
import sys
from pathlib import Path
import requests
import pycdlib

CACHE_SIZE = 4 * 1024 * 1024


class HttpRangeFile(io.RawIOBase):
    def __init__(self, url: str):
        self.url = url
        self.session = requests.Session()
        self.pos = 0
        self.cache_start = -1
        self.cache_data = b""

        r = self.session.get(
            url,
            headers={"Range": "bytes=0-0"},
            allow_redirects=True,
            timeout=30,
        )
        r.raise_for_status()

        if r.status_code != 206:
            raise RuntimeError(
                f"Server does not support Range: HTTP {r.status_code}"
            )

        cr = r.headers.get("Content-Range")
        if not cr:
            raise RuntimeError("No Content-Range header")

        self.size = int(cr.split("/")[-1])
        self.final_url = r.url

        print(f"ISO size : {self.size:,} bytes")
        print(f"CDN URL  : {self.final_url[:120]}...")

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            newpos = offset
        elif whence == io.SEEK_CUR:
            newpos = self.pos + offset
        elif whence == io.SEEK_END:
            newpos = self.size + offset
        else:
            raise ValueError("invalid whence")

        if newpos < 0:
            raise ValueError("negative seek")

        self.pos = newpos
        return self.pos

    def _fill_cache(self):
        start = self.pos
        end = min(start + CACHE_SIZE - 1, self.size - 1)

        r = self.session.get(
            self.final_url,
            headers={"Range": f"bytes={start}-{end}"},
            timeout=30,
        )

        if r.status_code != 206:
            raise IOError(f"Range request failed: HTTP {r.status_code}")

        self.cache_start = start
        self.cache_data = r.content
        print(f"Range GET: bytes={start}-{end} -> {len(self.cache_data):,} bytes")

    def read(self, size=-1):
        if self.pos >= self.size:
            return b""

        if size is None or size < 0:
            size = self.size - self.pos

        size = min(size, self.size - self.pos)
        output = bytearray()

        while size > 0:
            cache_end = self.cache_start + len(self.cache_data)

            if not (self.cache_start <= self.pos < cache_end):
                self._fill_cache()
                cache_end = self.cache_start + len(self.cache_data)

            offset = self.pos - self.cache_start
            available = len(self.cache_data) - offset
            n = min(size, available)

            output += self.cache_data[offset : offset + n]
            self.pos += n
            size -= n

        return bytes(output)


def main():
    if len(sys.argv) != 2:
        print("Usage:")
        print("  python remote_iso_test.py movie.strm")
        raise SystemExit(1)

    strm = Path(sys.argv[1])
    url = strm.read_text(encoding="utf-8-sig").strip()

    print("STRM:")
    print(strm)
    print("STRM URL:")
    print(url)

    remote = HttpRangeFile(url)
    iso = pycdlib.PyCdlib()

    print("\nParsing ISO/UDF...")
    iso.open_fp(remote)

    print("\n=== /BDMV/STREAM ===")
    count = 0
    for child in iso.list_children(udf_path="/BDMV/STREAM"):
        if child is None:
            continue
        count += 1
        try:
            path = iso.full_path_from_dirrecord(child)
            print(path)
        except Exception:
            print(child)

    print(f"\nEntries: {count}")
    iso.close()


if __name__ == "__main__":
    main()
