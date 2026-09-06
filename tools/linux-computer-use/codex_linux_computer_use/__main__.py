import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Native Linux computer-use MCP service"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Check local prerequisites without desktop sharing",
    )
    args = parser.parse_args()
    if args.doctor:
        from .doctor import main as diagnose

        raise SystemExit(diagnose())
    if sys.platform != "linux":
        raise SystemExit("Native Linux computer use requires a Linux desktop session.")

    from .server import create_server

    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
