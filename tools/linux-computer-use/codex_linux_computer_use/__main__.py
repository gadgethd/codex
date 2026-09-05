import sys

from .server import create_server

if sys.platform != "linux":
    raise SystemExit("Native Linux computer use requires a Linux desktop session.")

create_server().run(transport="stdio")
