"""Command line interface: check / preview / build / explain."""
from __future__ import annotations

import argparse
import sys

from . import checks, engine, explain, preview


def _cmd_check(args: argparse.Namespace) -> int:
    con = checks.open_input(args.input)
    try:
        errors = checks.run_all_checks(con)
    finally:
        con.close()
    if errors:
        print(f"CHECK FAILED with {len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(" - " + e, file=sys.stderr)
        return 1
    print("OK: catalog and all fact inputs passed every check")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    try:
        result = preview.preview(args.input, workdir=args.workdir)
    except engine.CheckFailed as exc:
        print("CHECK FAILED:", file=sys.stderr)
        for e in exc.errors:
            print(" - " + e, file=sys.stderr)
        return 1
    print(preview.render_report(result))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        meta = engine.build(args.input, args.output, workdir=args.workdir,
                            cache_mib=args.cache_mib)
    except engine.CheckFailed as exc:
        print("BUILD FAILED; no result was written:", file=sys.stderr)
        for e in exc.errors:
            print(" - " + e, file=sys.stderr)
        return 1
    print(f"built {args.output}")
    for k, v in meta.items():
        print(f"  {k}: {v}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    con = explain.open_result(args.result)
    try:
        if args.customer is None:
            for row in explain.list_cells(con):
                print(f"{row['new_customer']}\t{row['date_key']}\t"
                      f"{row['metric']}\tagg={row['agg_status']}\t"
                      f"distinct={row['distinct_status']}")
            print("\nshard recovery status:")
            for row in explain.shard_status(con):
                print(f"  {row['metric']} {row['shard_id']} ({row['form']}): "
                      f"agg={row['agg_status']} distinct="
                      f"{row['distinct_status']}")
        else:
            print(explain.explain_cell(con, args.customer, args.date,
                                       args.metric))
    finally:
        con.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rekey",
        description="Customer fact re-key tool (SQLite, disk-based)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("check", help="validate catalog + fact inputs")
    sp.add_argument("input")
    sp.set_defaults(func=_cmd_check)

    sp = sub.add_parser("preview", help="re-key into a throwaway DB and report")
    sp.add_argument("input")
    sp.add_argument("--workdir", default=None)
    sp.set_defaults(func=_cmd_preview)

    sp = sub.add_parser("build", help="write the new-customer-grain result")
    sp.add_argument("input")
    sp.add_argument("output")
    sp.add_argument("--workdir", default=None)
    sp.add_argument("--cache-mib", type=int, default=8,
                    help="SQLite page cache cap; lower forces disk spill")
    sp.set_defaults(func=_cmd_build)

    sp = sub.add_parser("explain", help="explain one result cell, or list all")
    sp.add_argument("result")
    sp.add_argument("--customer", default=None)
    sp.add_argument("--date", default=None)
    sp.add_argument("--metric", default=None)
    sp.set_defaults(func=_cmd_explain)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "explain" and args.customer is not None:
        if args.date is None or args.metric is None:
            print("explain requires --customer --date --metric together",
                  file=sys.stderr)
            return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
