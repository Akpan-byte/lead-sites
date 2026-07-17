#!/usr/bin/env python3
# CHANGE_SUMMARY
# 2026-07-17  kilo_regime_filter
#   - Background orchestrator that waits for VM, laptop, and GitHub Actions
#     workers to finish, collects their partials, then runs merge + quant_suite
#     + comparison report.
#   - Polls every 60 s; logs to regime_filter_run/logs/orchestrate.log.
# WHY: The full-IS run takes longer than one agent turn; this keeps the
#      finalize pipeline running unattended and writes the requested comparison
#      report as soon as all 125 chunked jobs are present.
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, Optional, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST = os.path.dirname(HERE)
LOG_DIR = os.path.join(HERE, "logs")
STATE_PATH = os.path.join(LOG_DIR, "orchestrate_state.json")
LOG_PATH = os.path.join(LOG_DIR, "orchestrate.log")
PARTIAL_DIR = os.path.join(HERE, "partials")
JOBS_PATH = os.path.join(HERE, "jobs.json")

LAPTOP_HOST = "akpan@100.93.22.56"
LAPTOP_DIR = "/home/akpan/kilo_regime_filter/backtest/regime_filter_run"
LAPTOP_PARTIAL_DIR = os.path.join(LAPTOP_DIR, "partials")

GHA_RUN_ID = "29575222994"
GHA_REPO = "Akpan-byte/lead-sites"

POLL_INTERVAL_S = 60
MAX_RUNTIME_S = 6 * 3600  # safety cap: 6 hours


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{ts} {msg}"
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    print(line, flush=True)


def _expected_jobs() -> Set[Tuple[str, int]]:
    with open(JOBS_PATH, "r", encoding="utf-8") as fh:
        jobs = json.load(fh)
    return {(j["variant"], j["chunk_idx"]) for j in jobs}


def _done_set(partial_dir: str) -> Set[Tuple[str, int]]:
    done: Set[Tuple[str, int]] = set()
    for f in glob.glob(os.path.join(partial_dir, "*_chunk*.summary.json")):
        base = os.path.basename(f).replace(".summary.json", "")
        if "_chunk" not in base:
            continue
        var, chunk_s = base.rsplit("_chunk", 1)
        try:
            done.add((var, int(chunk_s)))
        except ValueError:
            continue
    return done


def _load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(state: Dict[str, Any]) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)


def _laptop_status() -> dict:
    """Return {'done': int, 'missing': int} for the laptop partials dir."""
    cmd = [
        "tailscale", "ssh", LAPTOP_HOST,
        f"cd {LAPTOP_DIR} && python3 laptop_status.py",
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=30).decode().strip()
        return json.loads(out.splitlines()[-1])
    except Exception as e:
        _log(f"laptop status query failed: {e}")
        return {"done": -1, "missing": 9999}


def _collect_laptop() -> None:
    _log("collecting laptop partials")
    os.makedirs(PARTIAL_DIR, exist_ok=True)
    # Build a stable archive on the laptop first, then stream it once.
    archive = "/tmp/laptop_regime_partials.tar.gz"
    subprocess.run(
        ["tailscale", "ssh", LAPTOP_HOST,
         f"cd {LAPTOP_DIR} && tar -czf {archive} partials/"],
        check=True,
    )
    subprocess.run(
        f"tailscale ssh {LAPTOP_HOST} 'cat {archive}' | tar -xzf - -C {PARTIAL_DIR}",
        shell=True,
        check=True,
    )
    _log("laptop partials collected")


def _gha_status() -> Optional[str]:
    cmd = ["gh", "run", "view", GHA_RUN_ID, "--repo", GHA_REPO, "--json", "status,conclusion"]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=30).decode().strip()
        data = json.loads(out)
        status = data.get("status", "").lower()
        conclusion = data.get("conclusion", "").lower()
        if status == "completed":
            return conclusion or "completed"
        return status
    except Exception as e:
        _log(f"gha status query failed: {e}")
        return None


def _collect_gha() -> None:
    _log("collecting GitHub Actions partials")
    subprocess.run(
        [sys.executable, os.path.join(HERE, "collect_gha_partials.py"), GHA_RUN_ID],
        cwd=BACKTEST,
        check=True,
    )
    _log("github actions partials collected")


def _finalize() -> None:
    _log("all 125 chunked jobs present; running merge + quant + comparison")
    subprocess.run([sys.executable, os.path.join(HERE, "audit_partials.py")], cwd=BACKTEST, check=True)
    subprocess.run(
        [sys.executable, os.path.join(HERE, "merge.py"),
         "--partial-dir", PARTIAL_DIR,
         "--out-dir", os.path.join(BACKTEST, "results", "is_taker_regime_full")],
        cwd=BACKTEST,
        check=True,
    )
    subprocess.run(
        [sys.executable, os.path.join(HERE, "finalize.py"), "--workers", "4", "--fill", "taker"],
        cwd=BACKTEST,
        check=True,
    )
    _log("finalize complete")


def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    state = _load_state()
    expected = _expected_jobs()
    _log(f"orchestrator start: {len(expected)} expected jobs")

    start = time.time()
    laptop_collected = state.get("laptop_collected", False)
    gha_collected = state.get("gha_collected", False)
    finalized = state.get("finalized", False)

    while True:
        elapsed = time.time() - start
        if elapsed > MAX_RUNTIME_S:
            _log("orchestrator hit 6-hour safety cap; exiting")
            break

        local_done = _done_set(PARTIAL_DIR)
        local_missing = expected - local_done
        _log(f"local done={len(local_done)} missing={len(local_missing)} | "
             f"laptop_collected={laptop_collected} gha_collected={gha_collected}")

        if not laptop_collected:
            lap = _laptop_status()
            _log(f"laptop status = {lap}")
            if lap.get("missing", -1) == 0:
                try:
                    _collect_laptop()
                    laptop_collected = True
                except Exception as e:
                    _log(f"laptop collection failed: {e}")

        if not gha_collected:
            status = _gha_status()
            _log(f"gha status = {status}")
            if status in ("completed", "success", "failure"):
                try:
                    _collect_gha()
                    gha_collected = True
                except Exception as e:
                    _log(f"gha collection failed: {e}")

        if not finalized and len(local_missing) == 0:
            try:
                _finalize()
                finalized = True
                break
            except Exception as e:
                _log(f"finalize failed: {e}")

        state.update({
            "laptop_collected": laptop_collected,
            "gha_collected": gha_collected,
            "finalized": finalized,
            "last_local_done": len(local_done),
            "last_local_missing": len(local_missing),
        })
        _save_state(state)
        time.sleep(POLL_INTERVAL_S)

    _save_state(state)
    _log("orchestrator exit")


if __name__ == "__main__":
    main()
