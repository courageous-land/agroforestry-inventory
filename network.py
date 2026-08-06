"""Works around networks that advertise IPv6 without routing it.

The symptom is characteristic and misleading: the server publishes both IPv6 and
IPv4 addresses, the program tries IPv6 first, and with no route the connection is
not refused - it simply hangs until the timeout. The error you get is
"ConnectTimeout", which looks like a server outage or a firewall, when the same
address answers in milliseconds over IPv4.

Measured on such a network: 0.4 s over IPv4 against 19 s before giving up on IPv6.

The probe below runs once, costs at most two seconds, and only disables IPv6 when
IPv6 is genuinely broken. On a healthy network nothing changes - which matters,
because disabling IPv6 unnecessarily would break IPv6-only hosts.
"""

from __future__ import annotations

import socket

_adjusted = False


def _ipv6_works(host: str, port: int, budget: float) -> bool:
    """Complete a TLS handshake over IPv6 within the time budget.

    Testing only the TCP connect is not enough: on the network where this was
    measured, TCP completed normally and it was the TLS handshake that crawled,
    taking each request to 12 s instead of 0.4 s. A probe that stops at TCP
    returns the wrong verdict - which is exactly what happened here.
    """
    import ssl

    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror:
        return False          # no AAAA record: nothing to work around
    if not infos:
        return False

    context = ssl.create_default_context()
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(budget)
    try:
        s.connect(infos[0][4])
        with context.wrap_socket(s, server_hostname=host):
            return True
    except (OSError, ssl.SSLError):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def prefer_ipv4(host: str = "huggingface.co", port: int = 443, budget: float = 2.0) -> bool:
    """If IPv6 is not working, make Python resolve IPv4 only.

    Returns True if it had to intervene. Calling it again does nothing.
    """
    global _adjusted
    if _adjusted:
        return False
    _adjusted = True

    try:
        infos = socket.getaddrinfo(host, port)
    except socket.gaierror:
        return False
    if not any(fam == socket.AF_INET6 for fam, *_ in infos):
        return False          # the host offers no IPv6 at all
    if _ipv6_works(host, port, budget):
        return False          # IPv6 is fine: leave it alone

    original = socket.getaddrinfo

    def ipv4_only(host, port, family=0, *args, **kwargs):
        return original(host, port, socket.AF_INET, *args, **kwargs)

    socket.getaddrinfo = ipv4_only
    return True
