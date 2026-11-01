#!/usr/bin/env python3
"""Explicit historical research jobs; never trades or accesses legacy accounts."""
import argparse
import json
import os
from pathlib import Path

from archive_data import ArchiveStore, DATA_ROOT, protocol


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("fetch", "evaluate", "status", "protocol"))
    parser.add_argument("--data-root", type=Path, default=DATA_ROOT)
    args = parser.parse_args()
    try:
        os.nice(19)
    except (AttributeError, OSError):
        pass
    store = ArchiveStore(args.data_root)
    if args.command == "protocol":
        doc, digest = protocol(); print(json.dumps({"sha256": digest, "protocol": doc}, indent=2)); return
    if args.command == "status":
        print(json.dumps(store.manifest(), indent=2)); return
    def progress(value):
        print(json.dumps(value), flush=True)
    if args.command == "fetch":
        result = store.fetch(progress)
        print(json.dumps({"state": result["state"], "completed": result["completed"], "datasets": result["datasets"], "error": result.get("error")}), flush=True)
        if result["state"] != "verified": raise SystemExit(1)
    else:
        from walkforward import run_study
        result = run_study(store, progress=progress)
        print(json.dumps({"state": "complete", "study_id": result["study_id"], "protocol_sha256": result["protocol_sha256"]}), flush=True)


if __name__ == "__main__":
    main()
