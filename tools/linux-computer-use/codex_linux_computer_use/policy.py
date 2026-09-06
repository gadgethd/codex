"""Validate host-owned policy metadata; model arguments never grant access."""

from dataclasses import dataclass

from .dbus import PortalError

POLICY_KEY = "codex/linuxComputerUsePolicy"


@dataclass(frozen=True)
class LinuxPolicy:
    enabled: bool
    default_app_access: str
    desktop_ids: dict
    allow_locked_computer_use: bool

    @classmethod
    def from_meta(cls, meta):
        value = meta.get(POLICY_KEY) if isinstance(meta, dict) else None
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
        ):
            raise PortalError(
                "Native computer use requires Linux policy metadata from the Codex host."
            )
        apps = value.get("desktopIds")
        if (
            type(value.get("enabled")) is not bool
            or value.get("defaultAppAccess") not in ("allow", "deny")
            or type(value.get("allowLockedComputerUse")) is not bool
            or not isinstance(apps, dict)
            or len(apps) > 256
            or any(
                not isinstance(name, str)
                or len(name.encode("utf-8")) > 512
                or access not in ("allow", "deny")
                for name, access in apps.items()
            )
        ):
            raise PortalError(
                "The Codex host supplied invalid Linux computer-use policy."
            )
        return cls(
            value["enabled"],
            value["defaultAppAccess"],
            dict(apps),
            value["allowLockedComputerUse"],
        )

    def require_enabled(self):
        if not self.enabled:
            raise PortalError("Computer use is disabled by Codex policy.")

    @property
    def restricts_apps(self):
        return self.default_app_access != "allow" or "deny" in self.desktop_ids.values()

    def require_app(self, desktop_id):
        self.require_enabled()
        if desktop_id is None:
            if self.restricts_apps:
                raise PermissionError(
                    "Application identity is unknown under this policy."
                )
        elif self.desktop_ids.get(desktop_id, self.default_app_access) != "allow":
            raise PermissionError("Application access is denied by Codex policy.")

    def require_desktop(self):
        self.require_enabled()
        if self.restricts_apps:
            raise PortalError(
                "Full-desktop capture and input are unavailable under this application policy."
            )
