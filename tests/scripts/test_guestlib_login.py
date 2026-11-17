"""GuestConsole login: keep poking the console until a prompt appears (real CirrOS stays silent until
a newline arrives after the guest's getty is ready)."""

import pexpect

from scripts import guestlib


class FakeChild:
    """Scripted pexpect child. Each expect() consumes one scripted event: an index to return, or
    "timeout" to raise pexpect.TIMEOUT. sendline() records what the script sent."""

    def __init__(self, events):
        self.events = list(events)
        self.sent = []

    def sendline(self, s):
        self.sent.append(s)

    def expect(self, patterns, timeout=None):
        ev = self.events.pop(0)
        if ev == "timeout":
            raise pexpect.TIMEOUT("no prompt yet")
        return ev


def test_login_resends_newline_until_the_login_prompt_appears():
    # Guest silent for the first two windows, then "login:", password, shell prompt.
    child = FakeChild(["timeout", "timeout", 0, 1, 2])
    guestlib.login_with(child, "cirros", "gocubsgo")
    assert child.sent == ["", "", "", "cirros", "gocubsgo"]
    assert child.events == []  # every scripted event was consumed


def test_login_gives_up_after_the_total_window():
    child = FakeChild(["timeout"] * 50)
    try:
        guestlib.login_with(child, "cirros", "gocubsgo", attempts=3)
    except pexpect.TIMEOUT:
        pass
    else:
        raise AssertionError("expected TIMEOUT after the attempts ran out")
    assert child.sent == ["", "", ""]


def test_login_starts_over_when_the_password_step_fails():
    # Real case (2026-11-17): the username was lost before the guest was ready, the password was
    # then refused, and the login prompt came back. The whole login must be retried, not abandoned.
    child = FakeChild([0, 1, "timeout", 0, 1, 2])
    guestlib.login_with(child, "cirros", "gocubsgo")
    assert child.sent == ["", "cirros", "gocubsgo", "", "cirros", "gocubsgo"]
    assert child.events == []


def test_login_skips_password_when_already_at_a_shell():
    child = FakeChild([2])
    guestlib.login_with(child, "cirros", "gocubsgo")
    assert child.sent == [""]
