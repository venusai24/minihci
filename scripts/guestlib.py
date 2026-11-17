"""Guest console access for the owner scripts: `virsh console` through pexpect (CirrOS guests).

Only strip_echo is pure; GuestConsole needs a real libvirt domain and is never used by tests.
"""

# Anchored at the end of the buffer and allowing trailing whitespace: the banner says
# "default password: 'gocubsgo'." and must not match, and the console sends "\r\n" after a prompt.
LOGIN = r"login:\s*$"
PASSWORD = r"[Pp]assword:\s*$"
PROMPT = r"\$\s*$"


def strip_echo(cmd: str, raw: str) -> str:
    """Remove the echoed command line, the trailing shell prompt and carriage returns from console output."""
    lines = raw.replace("\r", "").split("\n")
    if lines and lines[0].strip() == cmd.strip():
        lines = lines[1:]
    if lines and lines[-1].strip() in ("$", "#", ""):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n")


def login_with(child, user: str, password: str, attempts: int = 12, window_s: float = 5) -> None:
    """Log in on an open console. The guest stays silent until a newline arrives after its getty is
    ready, so send one per window and wait for a prompt, for up to attempts * window_s seconds."""
    import pexpect

    for _ in range(attempts):
        child.sendline("")
        try:
            i = child.expect([LOGIN, PASSWORD, PROMPT], timeout=window_s)
        except pexpect.TIMEOUT:
            continue
        if i == 0:
            child.sendline(user)
            child.expect(PASSWORD, timeout=window_s)
            i = 1
        if i == 1:
            child.sendline(password)
            child.expect(PROMPT, timeout=window_s)
        return
    raise pexpect.TIMEOUT(f"no login or shell prompt after {attempts * window_s:g} s on the console")


class GuestConsole:
    def __init__(self, uri: str, domain: str, user: str = "cirros", password: str = "gocubsgo"):
        import pexpect

        self.child = pexpect.spawn("virsh", ["-c", uri, "console", domain, "--force"], encoding="utf-8", timeout=60)
        login_with(self.child, user, password)

    def run(self, cmd: str, timeout: int = 30) -> str:
        self.child.sendline(cmd)
        self.child.expect(PROMPT, timeout=timeout)
        return strip_echo(cmd, self.child.before)

    def close(self) -> None:
        self.child.close()
