"""Local Gateway CLI utility.

Section 4.7 Implementation:
Commands:
  gw status
  gw queue
  gw tags [--asset X]
  gw config validate
  gw config apply
  gw config versions
  gw config revert <v>
  gw uplink test
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.resolve()))

from gateway.src.store import GatewayStorage
from gateway.src.configsvc import ConfigService


def main():
    parser = argparse.ArgumentParser(description="VRA Edge Gateway CLI ('gw')")
    parser.add_argument("--db", default="gateway.db", help="Path to gateway SQLite DB")
    parser.add_argument("--config-dir", default="sites/VLK-ISL-001", help="Path to config directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # status
    subparsers.add_parser("status", help="Show gateway link & store status")

    # queue
    subparsers.add_parser("queue", help="Show outbox queue stats")

    # tags
    tags_parser = subparsers.add_parser("tags", help="Show current tag values")
    tags_parser.add_argument("--asset", help="Filter by asset ID")

    # config
    cfg_parser = subparsers.add_parser("config", help="Manage gateway configuration")
    cfg_sub = cfg_parser.add_subparsers(dest="cfg_action", required=True)
    cfg_sub.add_parser("validate", help="Validate current config files")
    cfg_sub.add_parser("versions", help="List config versions")
    revert_parser = cfg_sub.add_parser("revert", help="Revert to specific version")
    revert_parser.add_argument("version", help="Version name to revert to")

    args = parser.parse_args()
    db = GatewayStorage(Path(args.db))
    cfg_svc = ConfigService(Path(args.config_dir))

    if args.command == "status":
        stats = db.get_queue_stats()
        print("=== Gateway Status ===")
        print(f"Database: {db.db_path} (exists: {db.db_path.exists()})")
        print(f"Pending Outbox Batches: {stats['pending']}")
        print(f"Inflight Outbox Batches: {stats['inflight']}")
        print(f"Oldest Unsent Age: {stats['oldest_unsent_age_s']}s")
        print(f"Active Config Version: {cfg_svc.current_version}")

    elif args.command == "queue":
        stats = db.get_queue_stats()
        print(json.dumps(stats, indent=2))

    elif args.command == "tags":
        with db._get_connection() as conn:
            cursor = conn.cursor()
            if args.asset:
                cursor.execute("SELECT * FROM tag_state WHERE asset_id = ? ORDER BY tag", (args.asset,))
            else:
                cursor.execute("SELECT * FROM tag_state ORDER BY asset_id, tag")
            rows = cursor.fetchall()
            print(f"{'Asset':<12} | {'Tag':<15} | {'Value':<12} | {'Quality':<8} | {'Timestamp':<14}")
            print("-" * 70)
            for r in rows:
                val = r["value_num"] if r["value_num"] is not None else r["value_text"]
                print(f"{r['asset_id']:<12} | {r['tag']:<15} | {str(val):<12} | {r['quality']:<8} | {r['t_ms']:<14}")

    elif args.command == "config":
        if args.cfg_action == "validate":
            res = cfg_svc.validate_current()
            if res.is_valid:
                print("Config validation PASSED.")
            else:
                print(f"Config validation FAILED with {len(res.errors)} error(s):")
                for e in res.errors:
                    print(f"  [ERROR] {e}")
                sys.exit(1)
        elif args.cfg_action == "versions":
            versions = cfg_svc.list_versions()
            print(f"Versions ({len(versions)}):")
            for v in versions:
                current = " (active)" if v == cfg_svc.current_version else ""
                print(f"  - {v}{current}")
        elif args.cfg_action == "revert":
            ok, msg = cfg_svc.rollback_to_version(args.version)
            print(msg)
            sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
