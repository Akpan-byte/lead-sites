#!/usr/bin/env python3
"""Modal serverless offload for ETH/SOL Phase 1 taker backtests.

One image carries both coins' static files; the data volume supplies per-coin
raw tars which each container extracts to /tmp on first use. Results land in
the bt-results volume under results/<window>_taker_<coin>/.
"""
import modal
from pathlib import Path

app = modal.App("poly-taker-offload-coins")
data_vol = modal.Volume.from_name("bt-data", create_if_missing=True)
res_vol = modal.Volume.from_name("bt-results", create_if_missing=True)

HERE = Path(__file__).resolve().parent

# Static files needed by run_is.py and driver.py.
_static_dirs = [
    (str(HERE / "src"), "/root/bt/src"),
]
_static_files = [
    (str(HERE / "driver.py"), "/root/bt/driver.py"),
    (str(HERE / "run_is.py"), "/root/bt/run_is.py"),
    (str(HERE / "run_oos.py"), "/root/bt/run_oos.py"),
]
for coin in ("eth", "sol"):
    _static_files += [
        (str(HERE / f"modal_is_files_{coin}_compact.txt"), f"/root/bt/is_files_{coin}_compact.txt"),
        (str(HERE / f"modal_oos_files_{coin}_compact.txt"), f"/root/bt/oos_files_{coin}_compact.txt"),
        (str(HERE / f"coin_full_registry_{coin}.json"), f"/root/bt/coin_full_registry_{coin}.json"),
        (str(HERE / f"modal_batches_{coin}.json"), f"/root/bt/modal_batches_{coin}.json"),
    ]

image = modal.Image.debian_slim(python_version="3.11").pip_install("tzdata")
for local, remote in _static_dirs:
    if Path(local).is_dir():
        image = image.add_local_dir(local, remote_path=remote)
    else:
        print(f"WARNING: missing static dir {local}")
for local, remote in _static_files:
    if Path(local).is_file():
        image = image.add_local_file(local, remote)
    else:
        print(f"WARNING: missing static file {local}")


def _run_batch(coin: str, idx: int, window: str) -> list[str]:
    import json
    import os
    import shutil
    import sys

    os.environ["BT_ASSET"] = coin.upper()
    os.environ["BT_EXTRA_STRATEGIES"] = f"/root/bt/coin_full_registry_{coin}.json"
    os.environ["BT_REF_BTC_1M_DIR"] = f"/data/ref_{coin}_1m"
    sys.path.insert(0, "/root/bt")
    sys.path.insert(0, "/root/bt/src")

    compact_dir = f"/data/{coin}5m_compact"
    if not os.path.isdir(compact_dir):
        raise FileNotFoundError(f"compact dir not found: {compact_dir}")

    batches = json.load(open(f"/root/bt/modal_batches_{coin}.json"))
    out = []
    if window == "is":
        os.environ["BT_IS_DIR"] = f"is_taker_{coin}"
        shutil.copy(f"/data/is_index_{coin}_compact.json.gz", "/root/bt/is_index_compact.json.gz")
        os.makedirs(f"/root/bt/results/is_taker_{coin}", exist_ok=True)
        import run_is  # noqa: E402
        files = [l.strip() for l in open(f"/root/bt/is_files_{coin}_compact.txt") if l.strip()]
        for name in batches[idx]:
            try:
                out.append(run_is.run_strategy(name, files, True, "taker"))
            except Exception as exc:
                out.append(f"FAIL {name}: {exc}")
            res_vol.commit()
    else:
        os.environ["BT_OOS_DIR"] = f"oos_taker_{coin}"
        os.makedirs(f"/root/bt/results/oos_taker_{coin}", exist_ok=True)
        import run_oos  # noqa: E402
        files = [l.strip() for l in open(f"/root/bt/oos_files_{coin}_compact.txt") if l.strip()]
        for name in batches[idx]:
            try:
                out.append(run_oos.run_strategy_oos(name, files, "taker"))
            except Exception as exc:
                out.append(f"FAIL {name}: {exc}")
            res_vol.commit()
    return out


@app.function(image=image, cpu=2.0, memory=4096, timeout=3 * 3600,
              volumes={"/data": data_vol, "/root/bt/results": res_vol})
def run_batch_eth(idx: int, window: str) -> list[str]:
    return _run_batch("eth", idx, window)


@app.function(image=image, cpu=2.0, memory=4096, timeout=3 * 3600,
              volumes={"/data": data_vol, "/root/bt/results": res_vol})
def run_batch_sol(idx: int, window: str) -> list[str]:
    return _run_batch("sol", idx, window)


@app.local_entrypoint()
def main(coin: str = "eth", window: str = "is"):
    import json

    batches = json.load(open(f"/config/backtest/modal_batches_{coin}.json"))
    fn = run_batch_eth if coin == "eth" else run_batch_sol
    print(f"launching {len(batches)} {coin}/{window} batches", flush=True)
    for res in fn.map(range(len(batches)), window):
        for line in res:
            print(line, flush=True)
