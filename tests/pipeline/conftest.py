import socket

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Pipeline tests run on recorded responses; any real connection is a test bug."""

    def refuse(*_args, **_kwargs):
        raise RuntimeError("tests must not use the network (use the recorded fixtures)")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
