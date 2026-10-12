"""Read one case's data.csv out of RCAEval's Zenodo zip without downloading the zip, and
compare it with our Hugging Face metrics.parquet for the same case.

Why: RCAEval's published BARO numbers come from `main.py`, which reads `data.csv` from
the Zenodo zips; the Hugging Face Parquet is a conversion of `metrics.json`. If the two
differ (other columns, other rows), our port is right but the inputs are not the same.

A zip's index sits at its end, so HTTP range requests fetch only the index and the one
member (a few MB of a multi-GB archive). Stdlib only. Run on the VM:

    uv run python scripts/zenodo_peek.py RE2-TT ts-order-service_cpu 1
"""

from __future__ import annotations

import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ZENODO = "https://zenodo.org/records/14590730/files/{name}.zip?download=1"
BLOCK = 1 << 20


class HttpRangeFile(io.RawIOBase):
    """A read-only, seekable file over HTTP range requests, in cached 1 MB blocks."""

    def __init__(self, url: str) -> None:
        self.url = url
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=60) as r:
            self.size = int(r.headers["Content-Length"])
            self.final_url = r.url  # follow Zenodo's redirect once
        self.pos = 0
        self.blocks: dict[int, bytes] = {}
        self.fetched = 0

    def _block(self, i: int) -> bytes:
        if i not in self.blocks:
            start, end = i * BLOCK, min((i + 1) * BLOCK, self.size) - 1
            req = urllib.request.Request(self.final_url, headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=120) as r:
                if r.status != 206:
                    raise SystemExit(f"server ignored the Range header (HTTP {r.status})")
                self.blocks[i] = r.read()
            self.fetched += len(self.blocks[i])
        return self.blocks[i]

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = base + offset
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


def main() -> None:
    dataset, case_dir, rep = sys.argv[1], sys.argv[2], sys.argv[3]  # RE2-TT ts-order-service_cpu 1
    f = HttpRangeFile(ZENODO.format(name=dataset))
    z = zipfile.ZipFile(f)  # type: ignore[arg-type]
    # The folder layout inside the zip is inferred from main.py; match the case anywhere.
    needle = f"/{case_dir}/{rep}/"
    members = [m for m in z.namelist() if needle in "/" + m and not m.endswith("/")]
    if not members:
        print(f"no files under {needle}; first entries: {z.namelist()[:8]}")
        return
    prefix = members[0][: ("/" + members[0]).index(needle) + len(needle) - 1]
    print(f"zip {f.size / 1e9:.1f} GB; files in {prefix}:")
    for m in members:
        print(f"  {m[len(prefix):]:<24} {z.getinfo(m).file_size:>12,} bytes")
    if prefix + "data.csv" not in members:
        print("no data.csv in this case; main.py would fall back to simple_metrics.csv")
        return
    with z.open(prefix + "data.csv") as fh:
        csv = io.TextIOWrapper(fh, encoding="utf-8").read()
    lines = csv.splitlines()
    header = lines[0].split(",")
    print(f"data.csv: {len(lines) - 1} rows, {len(header)} columns; fetched {f.fetched / 1e6:.1f} MB")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import pyarrow.parquet as pq

    from yi_agent.config import load_settings

    name = f"{dataset[:3].lower()}{dataset[-2:].lower()}_{case_dir}_{rep}"
    pq_path = load_settings().data_dir / name / "metrics.parquet"
    ours = pq.read_table(pq_path)
    cols = set(ours.column_names)
    theirs = set(header)
    fam = lambda names: sorted({n.split("_", 1)[1] for n in names if "_" in n})  # noqa: E731
    print(f"metrics.parquet ({name}): {ours.num_rows} rows, {len(cols)} columns")
    print(f"  families in data.csv:       {fam(theirs)}")
    print(f"  families in metrics.parquet: {fam(cols)}")
    only_theirs, only_ours = sorted(theirs - cols), sorted(cols - theirs)
    print(f"  only in data.csv ({len(only_theirs)}): {only_theirs[:12]}")
    print(f"  only in metrics.parquet ({len(only_ours)}): {only_ours[:12]}")
    # Same values where both have the column? Compare the first 200 rows by time.
    t_idx = header.index("time")
    rows = [line.split(",") for line in lines[1:201]]
    ours_by_time = {int(t): i for i, t in enumerate(ours.column("time").to_pylist())}
    same = diff = missing = 0
    for c in sorted(theirs & cols - {"time"})[:40]:
        j = header.index(c)
        col = ours.column(c).to_pylist()
        for r in rows:
            i = ours_by_time.get(int(float(r[t_idx])))
            if i is None or r[j] == "":
                missing += 1
                continue
            a, b = float(r[j]), col[i]
            if b is not None and abs(a - b) <= 1e-9 * max(1.0, abs(a)):
                same += 1
            else:
                diff += 1
    print(f"  values (40 shared columns x 200 rows): {same} equal, {diff} differ, {missing} unmatched")


if __name__ == "__main__":
    main()
