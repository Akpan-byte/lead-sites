#!/usr/bin/env python3
import glob, json, os

HERE = os.path.dirname(os.path.abspath(__file__))
jobs = json.load(open(os.path.join(HERE, "jobs.json")))
expected = {(j["variant"], j["chunk_idx"]) for j in jobs}
done = set()
for f in glob.glob(os.path.join(HERE, "partials", "*_chunk*.summary.json")):
    base = os.path.basename(f).replace(".summary.json", "")
    if "_chunk" not in base:
        continue
    var, chunk_s = base.rsplit("_chunk", 1)
    try:
        done.add((var, int(chunk_s)))
    except ValueError:
        continue
missing = expected - done
print(json.dumps({"done": len(done), "missing": len(missing)}))
