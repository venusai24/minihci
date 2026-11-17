"""Guest console access for the owner scripts: `virsh console` through pexpect (CirrOS guests).

Only strip_echo is pure; GuestConsole needs a real libvirt domain and is never used by tests.
"""

PROMPT = r"\$ $"


def strip_echo(cmd: str, raw: str) -> str:
    """Remove the echoed command line, the trailing shell prompt and carriage returns from console output."""
    lines = raw.replace("\r", "").split("\n")
    if lines and lines[0].strip() == cmd.strip():
        lines = lines[1:]
    if lines and lines[-1].strip() in ("$", "#", ""):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


class GuestConsole:
    def __init__(self, uri: str, domain: str, user: str = "cirros", password: str = "gocubsgo"):
        import pexpect

        self.child = pexpect.spawn("virsh", ["-c", uri, "console", domain, "--force"], encoding="utf-8", timeout=60)
        self._login(user, password)

    def _login(self, user: str, password: str) -> None:
        self.child.sendline("")
        i = self.child.expect([r"login: ?$", r"[Pp]assword: ?$", PROMPT])
        if i == 0:
            self.child.sendline(user)
            self.child.expect(r"[Pp]assword: ?$")
            i = 1
        if i == 1:
            self.child.sendline(password)
            self.child.expect(PROMPT)

    def run(self, cmd: str, timeout: int = 30) -> str:
        self.child.sendline(cmd)
        self.child.expect(PROMPT, timeout=timeout)
        return strip_echo(cmd, self.child.before)

    def close(self) -> None:
        self.child.close()
