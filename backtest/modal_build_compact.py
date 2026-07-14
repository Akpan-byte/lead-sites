#!/usr/bin/env python3
"""Modal job: extract raw tar, build compact files, build index, save to volume.

Usage (local entrypoint):
  modal run modal_build_compact.py --coin eth
"""
import modal
from pathlib import Path

app = modal.App("build-compact-coins")
data_vol = modal.Volume.from_name("bt-data", create_if_missing=True)

HERE = Path(__file__).resolve().parent

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("tzdata")
    .add_local_dir(str(HERE / "src"), remote_path="/root/bt/src")
    .add_local_file(str(HERE / "driver.py"), remote_path="/root/bt/driver.py")
    .add_local_file(str(HERE / "compactify_coin.py"), remote_path="/root/bt/compactify_coin.py")
    .add_local_file(str(HERE / "build_index_coin_compact.py"), remote_path="/root/bt/build_index_coin_compact.py")
)


@app.function(image=image, cpu=2.0, memory=4096, timeout=2 * 3600,
              volumes={"/data": data_vol})
def build_compact(coin: str) -> str:
    import os
    import subprocess
    import sys
    import shutil

    sys.path.insert(0, "/root/bt")
    sys.path.insert(0, "/root/bt/src")

    raw_dir = f"/data/{coin}5m_all"
    compact_dir = f"/data/{coin}5m_compact"
    ref_dir = f"/data/ref_{coin}_1m"

    # Extract raw data tar if not already present.
    if not os.path.isdir(raw_dir):
        subprocess.run(["tar", "xf", f"/data/{coin}data.tar", "-C", "/data"], check=True)

    # Extract ref klines tar if not already present.
    if not os.path.isdir(ref_dir):
        subprocess.run(["tar", "xf", f"/data/ref_{coin}_1m.tar", "-C", "/data"], check=True)

    # Build compact files.
    price_field = "eth_price" if coin == "eth" else "sol_price"
    import compactify_coin
    sys.argv = ["compactify_coin.py", coin, raw_dir, compact_dir, price_field, "--workers", "2"]
    compactify_coin.main()

    # Build indexes for IS and OOS from all compact files.
    import build_index_coin_compact
    for window in ("is", "oos"):
        out_idx = f"/data/{window}_index_{coin}_compact.json.gz"
        sys.argv = ["build_index_coin_compact.py", coin, compact_dir, out_idx, "--workers", "2"]
        build_index_coin_compact.main()

    # Optionally remove raw dir to save volume space (keep tar as backup).
    shutil.rmtree(raw_dir, ignore_errors=True)

    n_compact = len(list(Path(compact_dir).glob("*.pkl.gz")))
    return f"{coin}: compacted {n_compact} files to {compact_dir}"


@app.local_entrypoint()
def main(coin: str = "eth"):
    print(build_compact.remote(coin))
