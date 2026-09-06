"""Transfer effective policy through a private inherited file descriptor."""

import json
import os
import tempfile
from contextlib import contextmanager

from .policy import POLICY_KEY, LinuxPolicy

MAX_POLICY_BYTES = 1048576


@contextmanager
def policy_file(policy):
    meta = (
        {}
        if policy is None
        else {
            POLICY_KEY: {
                "version": 1,
                "enabled": policy.enabled,
                "defaultAppAccess": policy.default_app_access,
                "desktopIds": policy.desktop_ids,
                "allowLockedComputerUse": policy.allow_locked_computer_use,
            }
        }
    )
    data = json.dumps(meta).encode()
    if len(data) > MAX_POLICY_BYTES:
        raise ValueError("Worker policy exceeds its size limit.")
    with tempfile.TemporaryFile() as stream:
        stream.write(data)
        stream.seek(0)
        yield stream.fileno()


def read_policy(descriptor):
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        data = stream.read(MAX_POLICY_BYTES + 1)
    if len(data) > MAX_POLICY_BYTES:
        raise ValueError("Worker policy exceeds its size limit.")
    return LinuxPolicy.from_meta(json.loads(data))
