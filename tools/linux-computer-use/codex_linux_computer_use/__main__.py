import sys


def main():
    if sys.platform != "linux":
        raise SystemExit("Native Linux computer use requires a Linux desktop session.")

    from .server import create_server

    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
