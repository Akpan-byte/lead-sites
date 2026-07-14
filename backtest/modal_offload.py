# CHANGE_SUMMARY
# 2026-07-12  kimi
#   - Modal serverless offload for the is_taker backtest tail (42 queued strats,
#     9 balanced batches). Reuses run_is.run_strategy verbatim: same code, same
#     data, same fill logic as the VM/laptop runs -> results are merge-identical.
#   - Data ships as a single tar in the bt-data volume; each container extracts
#     to /tmp so the original /tmp/btc5m_compact paths work unmodified.
#   - tzdata pip-installed: debian_slim lacks the tz database and every strat
#     anchors to America/New_York.
# WHY: laptop OOM'd at 12 workers (WSL 15GB cap) and the taker fill path costs
#      3-6x instant on trigger-heavy strats; Modal gives us 9 extra workers with
#      no RAM ceiling so the queued tail finishes in parallel instead of serially.
import modal

app = modal.App("poly-taker-offload")
data_vol = modal.Volume.from_name("bt-data", create_if_missing=True)
res_vol = modal.Volume.from_name("bt-results", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("tzdata")  # ZoneInfo("America/New_York") needs the tz database
    .add_local_dir("/config/backtest/src", remote_path="/root/bt/src")
    .add_local_file("/config/backtest/driver.py", remote_path="/root/bt/driver.py")
    .add_local_file("/config/backtest/run_is.py", remote_path="/root/bt/run_is.py")
    .add_local_file("/config/backtest/is_files_compact.txt",
                    remote_path="/root/bt/is_files_compact.txt")
    .add_local_file("/config/backtest/is_index_compact.json.gz",
                    remote_path="/root/bt/is_index_compact.json.gz")
    .add_local_file("/config/backtest/modal_batches.json",
                    remote_path="/root/bt/modal_batches.json")
)


@app.function(image=image, cpu=2.0, memory=4096, timeout=3 * 3600,
              volumes={"/data": data_vol, "/root/bt/results": res_vol})
def run_batch(idx: int) -> list[str]:
    import json
    import os
    import subprocess
    import sys

    os.environ["BT_IS_DIR"] = "is_taker"
    sys.path.insert(0, "/root/bt")
    sys.path.insert(0, "/root/bt/src")

    # one-time data extraction per container (~274MB tar -> /tmp, original paths)
    if not os.path.isdir("/tmp/btc5m_compact"):
        subprocess.run(["tar", "xf", "/data/btdata.tar", "-C", "/tmp"], check=True)

    os.makedirs("/root/bt/results/is_taker", exist_ok=True)
    import run_is  # noqa: E402  (imports driver, bt_orb, bt_bars, bt_reference)

    files = [l.strip() for l in open("/root/bt/is_files_compact.txt") if l.strip()]
    batches = json.load(open("/root/bt/modal_batches.json"))
    out = []
    for name in batches[idx]:
        try:
            out.append(run_is.run_strategy(name, files, True, "taker"))
        except Exception as exc:  # keep the batch going; failures are visible
            out.append(f"FAIL {name}: {exc}")
        res_vol.commit()
    return out


@app.local_entrypoint()
def main() -> None:
    import json

    batches = json.load(open("/config/backtest/modal_batches.json"))
    print(f"launching {len(batches)} batches", flush=True)
    for res in run_batch.map(range(len(batches))):
        for line in res:
            print(line, flush=True)
