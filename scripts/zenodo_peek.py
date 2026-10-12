"""Read single files out of RCAEval's Zenodo zips without downloading the zips.

Why: RCAEval's published BARO numbers come from `main.py`, which reads each case's
`data.csv`, or, when there is none (RE2: there is none), `simple_metrics.csv`. Our
Hugging Face Parquet is a conversion of the full metrics. A zip's index sits at its end,
so HTTP range requests fetch only the index and the members asked for. Stdlib only.

    # one case: list its files, and diff simple_metrics.csv / metrics.csv with our Parquet
    uv run python scripts/zenodo_peek.py peek RE2-TT ts-order-service_cpu 1
    # every case: extract simple_metrics.csv (+ inject_time.txt) under OUT/<case name>/
    uv run python scripts/zenodo_peek.py extract RE2-TT simple_metrics.csv ~/yi-zenodo
"""

from __future__ import annotations

import http.client
import io
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ZENODO = "https://zenodo.org/records/14590730/files/{name}.zip?download=1"
BLOCK = 1 << 20
RETRIES = 5
TIMEOUT_S = 30  # per socket read: a stalled transfer fails fast and is retried


class HttpRangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests, in cached 1 MB blocks."""

    def __init__(self, url: str) -> None:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
            self.url = r.url  # follow Zenodo's redirect once
        self.pos = 0
        self.blocks: dict[int, bytes] = {}
        self.fetched = 0

    def _get(self, start: int, end: int) -> bytes:
        for attempt in range(RETRIES):
            why = "short read"
            try:
                req = urllib.request.Request(self.url, headers={"Range": f"bytes={start}-{end}"})
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                    if r.status != 206:
                        raise SystemExit(f"server ignored the Range header (HTTP {r.status})")
                    data: bytes = r.read()
                if len(data) == end - start + 1:
                    return data
            except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError, OSError) as e:
                why = repr(e)  # HTTP 429 (Zenodo's rate limit) is a URLError too
            wait = 2 ** (attempt + 2)  # a dropped connection is common on large archives
            print(f"  bytes {start}-{end}: {why}; retry in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
        raise SystemExit(f"bytes {start}-{end}: failed {RETRIES} times")

    def _block(self, i: int) -> bytes:
        if i not in self.blocks:
            start, end = i * BLOCK, min((i + 1) * BLOCK, self.size) - 1
            self.blocks[i] = self._get(start, end)
            self.fetched += len(self.blocks[i])
            if self.fetched // (8 * BLOCK) != (self.fetched - len(self.blocks[i])) // (8 * BLOCK):
                print(f"  {self.fetched / 1e6:.0f} MB fetched", file=sys.stderr, flush=True)
            if len(self.blocks) > 64:  # members are read front to back: keep memory bounded
                self.blocks.pop(next(iter(self.blocks)))
        return self.blocks[i]

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        self.pos = {0: 0, 1: self.pos, 2: self.size}[whence] + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            n = self.size - self.pos
        out = bytearray()
        while n > 0 and self.pos < self.size:
            i, off = divmod(self.pos, BLOCK)
            chunk = self._block(i)[off : off + n]
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)


def open_zip(dataset: str) -> tuple[HttpRangeFile, zipfile.ZipFile]:
    f = HttpRangeFile(ZENODO.format(name=dataset))
    return f, zipfile.ZipFile(f)  # type: ignore[arg-type]


def case_name(dataset: str, case_dir: str, rep: str) -> str:
    """RE2-TT, ts-order-service_cpu, 1 -> re2tt_ts-order-service_cpu_1 (our labels)."""
    return f"{dataset[:3].lower()}{dataset[-2:].lower()}_{case_dir}_{rep}"


def cases(z: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    """(case dir, rep) -> member prefix, from paths like RE2-TT/<svc>_<fault>/<rep>/<file>."""
    out = {}
    for m in z.namelist():
        parts = m.split("/")
        if len(parts) >= 4 and parts[-1] == "inject_time.txt":
            out[(parts[-3], parts[-2])] = "/".join(parts[:-1]) + "/"
    return out


def families(names: set[str]) -> list[str]:
    return sorted({n.split("_", 1)[1] for n in names if "_" in n})


def compare(label: str, csv_text: str, parquet_path: Path) -> None:
    import pyarrow.parquet as pq

    lines = csv_text.splitlines()
    header = lines[0].split(",")
    ours = pq.read_table(parquet_path)
    theirs, cols = set(header), set(ours.column_names)
    print(f"  {label}: {len(lines) - 1} rows, {len(header)} columns; families {families(theirs)}")
    print(f"    only in {label} ({len(theirs - cols)}): {sorted(theirs - cols)[:10]}")
    print(f"    only in our parquet ({len(cols - theirs)}): {sorted(cols - theirs)[:10]}")
    t_idx = header.index("time") if "time" in header else 0
    times = ours.column("time").to_pylist()
    ours_by_time = {int(t): i for i, t in enumerate(times)}
    rows = [line.split(",") for line in lines[1:301]]
    same = diff = unmatched = 0
    examples: list[str] = []
    for c in sorted(theirs & cols - {"time"})[:60]:
        j = header.index(c)
        col = ours.column(c).to_pylist()
        for r in rows:
            i = ours_by_time.get(int(float(r[t_idx])))
            if i is None or r[j] == "" or col[i] is None:
                unmatched += 1
                continue
            a, b = float(r[j]), float(col[i])
            if abs(a - b) <= 1e-9 * max(1.0, abs(a)):
                same += 1
            else:
                diff += 1
                if len(examples) < 3:
                    examples.append(f"{c} at {r[t_idx]}: csv {a} vs parquet {b}")
    print(
        f"    values (60 shared columns x 300 rows): {same} equal, {diff} differ, "
        f"{unmatched} unmatched"
    )
    for e in examples:
        print(f"      e.g. {e}")


def peek(dataset: str, case_dir: str, rep: str) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from yi_agent.config import load_settings

    f, z = open_zip(dataset)
    prefix = cases(z).get((case_dir, rep))
    if prefix is None:
        raise SystemExit(f"no case {case_dir}/{rep} in {dataset}")
    print(f"zip {f.size / 1e9:.1f} GB; {prefix}:")
    for m in z.namelist():
        if m.startswith(prefix) and not m.endswith("/"):
            print(f"  {m[len(prefix) :]:<24} {z.getinfo(m).file_size:>12,} bytes")
    parquet = load_settings().data_dir / case_name(dataset, case_dir, rep) / "metrics.parquet"
    for member in ("simple_metrics.csv", "metrics.csv"):
        if prefix + member in z.namelist():
            with z.open(prefix + member) as fh:
                compare(member, io.TextIOWrapper(fh, encoding="utf-8").read(), parquet)
    print(f"fetched {f.fetched / 1e6:.1f} MB")


def extract(dataset: str, member: str, out: Path) -> None:
    f, z = open_zip(dataset)
    found = cases(z)
    for n, ((case_dir, rep), prefix) in enumerate(sorted(found.items()), 1):
        dest = out / case_name(dataset, case_dir, rep)
        names = (member, "inject_time.txt")
        if all((dest / name).is_file() for name in names):
            continue  # resumable: a case counts as done only with both files
        data = {}
        for name in names:
            with z.open(prefix + name) as src:
                data[name] = src.read()
        dest.mkdir(parents=True, exist_ok=True)
        for name in names:
            (dest / (name + ".part")).write_bytes(data[name])
            (dest / (name + ".part")).rename(dest / name)
        print(f"[{n}/{len(found)}] {dest.name}  ({f.fetched / 1e6:.0f} MB fetched)", flush=True)
    print(f"done: {len(found)} cases under {out}")


if __name__ == "__main__":
    cmd, *rest = sys.argv[1:]
    if cmd == "peek":
        peek(*rest)
    elif cmd == "extract":
        extract(rest[0], rest[1], Path(rest[2]).expanduser())
    else:
        raise SystemExit(__doc__)
