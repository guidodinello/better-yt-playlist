"""CLI entry point."""

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Better YT Playlist")
    parser.add_argument("--version", action="version", version="0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("fetch", help="Fetch videos from a playlist")
    subparsers.add_parser("analyze", help="Analyze/cluster videos")

    args = parser.parse_args()

    if args.command == "fetch":
        print("fetch: not implemented yet")
    elif args.command == "analyze":
        print("analyze: not implemented yet")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
