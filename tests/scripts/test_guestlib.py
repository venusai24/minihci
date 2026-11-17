from scripts import guestlib


def test_strip_echo_removes_echo_prompt_and_cr():
    raw = "tail -1 /root/marks\r\n1700000042\r\n$ "
    assert guestlib.strip_echo("tail -1 /root/marks", raw) == "1700000042"


def test_strip_echo_multiline_output():
    raw = "ls\r\na\r\nb\r\n$ "
    assert guestlib.strip_echo("ls", raw) == "a\nb"


def test_strip_echo_without_echo_or_prompt():
    assert guestlib.strip_echo("true", "") == ""
    assert guestlib.strip_echo("x", "hello\r\n") == "hello"
