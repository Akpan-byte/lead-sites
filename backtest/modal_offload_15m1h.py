# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Modal offload for the correctly-sized ORB reruns, mirroring
#     modal_offload.py. Ships /tmp/btc15m_compact + /tmp/btc1h_compact as one
#     tar in the bt-data volume (btdata_15m1h.tar); each container extracts to
#     /tmp so is_files_{15m,1h}.txt paths resolve unmodified. Imports
#     run_orb_15m1h (not run_is directly) so the tf_hint registry patch is
#     applied inside the container, then reuses run_is.run_strategy verbatim.
#   - Batches: 5 x [one btc_orb_1h_* strat] on 1h markets by default; the VM
#     concurrently runs the 15m+30m families. Results land in the bt-results
#     volume under is_taker_15m1h/ and are pulled back with `modal volume get`.
# WHY: the 1h/15m compact sets are new data the original btdata.tar lacks, and
#      the empty-trade fix requires the tf_hint patch to run inside the worker
#      process (forkserver re-imports modules).
import modal

app = modal.App("poly-orb-15m1h-offload")
data_vol = modal.Volume.from_name("bt-data", create_if_missing=True)
res_vol = modal.Volume.from_name("bt-results", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("tzdata")  # ZoneInfo("America/New_York") needs the tz database
    .add_local_dir("/config/backtest/src", remote_path="/root/bt/src")
    .add_local_file("/config/backtest/driver.py", remote_path="/root/bt/driver.py")
    .add_local_file("/config/backtest/run_is.py", remote_path="/root/bt/run_is.py")
    .add_local_file("/config/backtest/run_orb_15m1h.py",
                    remote_path="/root/bt/run_orb_15m1h.py")
    .add_local_file("/config/backtest/is_files_15m.txt",
                    remote_path="/root/bt/is_files_15m.txt")
    .add_local_file("/config/backtest/is_files_1h.txt",
                    remote_path="/root/bt/is_files_1h.txt")
)

BATCHES = [
    ["phase_2.btc_orb_1h_1re"],
    ["phase_2.btc_orb_1h_5re"],
    ["phase_2.btc_orb_1h_12re"],
    ["phase_2.btc_orb_1h_50re"],
    ["phase_2.btc_orb_1h_unl"],
]


@app.function(image=image, cpu=2.0, memory=4096, timeout=3 * 3600,
              volumes={"/data": data_vol, "/root/bt/results": res_vol})
def run_batch(idx: int) -> list[str]:
    import os
    import subprocess
    import sys

    os.environ["BT_IS_DIR"] = "is_taker_15m1h"
    os.environ["ORB_TF_HINT"] = "1h"
    os.environ["ORB_FAMS"] = "1h"
    sys.path.insert(0, "/root/bt")
    sys.path.insert(0, "/root/bt/src")

    # one-time data extraction per container (~100MB tar -> /tmp, original paths)
    if not os.path.isdir("/tmp/btc1h_compact"):
        subprocess.run(["tar", "xf", "/data/btdata_15m1h.tar", "-C", "/tmp"],
                       check=True)

    import run_orb_15m1h  # noqa: E402  (patches registry, imports run_is)
    import run_is  # noqa: E402

    files = [l.strip() for l in open("/root/bt/is_files_1h.txt") if l.strip()]
    out = [f"patched: {len(run_orb_15m1h._PATCHED)} strats tf_hint=1h"]
    for name in BATCHES[idx]:
        try:
            out.append(run_is.run_strategy(name, files, True, "taker"))
        except Exception as exc:  # keep the batch going; failures are visible
            out.append(f"FAIL {name}: {exc}")
        res_vol.commit()
    return out


@app.local_entrypoint()
def main() -> None:
    print(f"launching {len(BATCHES)} batches", flush=True)
    for res in run_batch.map(range(len(BATCHES))):
        for line in res:
            print(line, flush=True)
