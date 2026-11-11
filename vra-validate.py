#!/usr/bin/env python3
"""CLI utility to validate site configs and export JSON schemas."""

import argparse
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from contracts.src.validate import validate_site_directory, export_all_json_schemas


def main():
    parser = argparse.ArgumentParser(description="VRA Digital Twin Contract Validation & Tooling")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate a site directory")
    validate_parser.add_argument("site_dir", type=str, help="Path to site directory (e.g. sites/VLK-ISL-001)")

    export_parser = subparsers.add_parser("export-schemas", help="Export JSON schemas")
    export_parser.add_argument("--out", type=str, default="contracts/jsonschema", help="Output directory")

    args = parser.parse_args()

    if args.command == "validate":
        site_path = Path(args.site_dir)
        print(f"Validating site directory: {site_path} ...")
        result = validate_site_directory(site_path)
        if result.warnings:
            for w in result.warnings:
                print(f"  [WARN] {w}")
        if result.errors:
            for e in result.errors:
                print(f"  [FAIL] {e}")
            print(f"Validation FAILED with {len(result.errors)} error(s).")
            sys.exit(1)
        else:
            print("Validation PASSED.")
            sys.exit(0)

    elif args.command == "export-schemas":
        out_path = Path(args.out)
        print(f"Exporting JSON Schemas to {out_path} ...")
        export_all_json_schemas(out_path)
        print("Done.")


if __name__ == "__main__":
    main()
