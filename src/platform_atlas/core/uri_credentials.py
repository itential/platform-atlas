"""Shared helper for percent-encoding raw credentials in connection URIs.

Users of ``env create`` paste a full MongoDB/Redis connection URI, including
the password, as free text. Passwords legitimately contain URL-reserved
characters (``#``, ``@``, ``/``, ``?``, ``:``) that the URI spec requires to
be percent-encoded when they appear in the userinfo component. Left raw, they
break ``urllib.parse.urlparse`` in a surprising way: the ``#`` is read as the
start of a fragment *before* urlparse ever looks for the host/port, so the
real host:port ends up inside the discarded "fragment" and accessing
``.port`` raises ``ValueError: Port could not be cast to integer value as
'<password>'`` — a confusing crash that has nothing to do with the port.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = ["encode_uri_credentials", "parse_uri_host", "retarget_uri_host"]


def encode_uri_credentials(uri: str) -> str:
    """Percent-encode the username/password portion of a connection URI.

    Splits the URI by hand instead of parsing with ``urlparse``: separates
    userinfo from the rest of the authority on the LAST ``@`` in the string
    (so a raw ``@`` in the password is preserved rather than misread as the
    userinfo/host boundary), percent-encodes the username/password, and
    reattaches everything after that ``@`` unchanged — host, port,
    multi-host seed lists, path, query, and fragment are never inspected.

    Returns the URI unchanged if there is no ``scheme://`` prefix, no ``@``
    (no credentials to encode), or an empty userinfo.
    """
    scheme, sep, remainder = uri.partition("://")
    if not sep:
        return uri

    userinfo, at, hostpart = remainder.rpartition("@")
    if not at:
        return uri

    username, _, password = userinfo.partition(":")
    # ``quote`` with an empty ``safe`` (not ``quote_plus``) is the correct
    # userinfo encoding: ``quote_plus`` renders a space as ``+``, which pymongo
    # decodes back to a space (``unquote_plus``) but redis-py does not
    # (``unquote``) — so a password containing a space would silently fail Redis
    # auth. ``quote`` percent-encodes the space, which both drivers decode.
    encoded_username = quote(username, safe="") if username else ""
    encoded_password = quote(password, safe="") if password else ""

    if encoded_password:
        credentials = f"{encoded_username}:{encoded_password}"
    elif encoded_username:
        credentials = encoded_username
    else:
        return uri

    return f"{scheme}://{credentials}@{hostpart}"


def _split_netloc(hostpart: str) -> tuple[str, str]:
    """Split ``host:port/path?query#fragment`` into ``(netloc, rest)``."""
    idx = len(hostpart)
    for ch in ("/", "?", "#"):
        pos = hostpart.find(ch)
        if pos != -1:
            idx = min(idx, pos)
    return hostpart[:idx], hostpart[idx:]


def _parse_uri_authority(uri: str) -> tuple[str, str, str, int, str]:
    """Split a connection URI into ``(scheme, userinfo_prefix, host, port, rest)``.

    ``userinfo_prefix`` is ``"user:pass@"`` or ``""``. ``rest`` is everything
    after ``host:port`` — path, query, and fragment, untouched. Shared by
    :func:`parse_uri_host` and :func:`retarget_uri_host`.

    Raises ``ValueError`` if the URI can't be safely tunneled:

    - ``+srv`` schemes resolve hosts dynamically via DNS SRV lookup and can't
      follow a static port-forward.
    - Multi-host seed lists (replica sets) would each need their own forward —
      unsupported for now.
    - A missing port can't be forwarded without knowing what to forward to.
    """
    scheme, sep, remainder = uri.partition("://")
    if not sep:
        raise ValueError(f"Not a valid connection URI (missing '://'): {uri!r}")
    if "+srv" in scheme.lower():
        raise ValueError(
            f"'{scheme}' URIs resolve hosts via DNS SRV lookup and cannot be tunneled "
            "through a jumphost — use a direct URI with an explicit host:port instead"
        )

    userinfo, at, hostpart = remainder.rpartition("@")
    prefix = f"{userinfo}@" if at else ""

    netloc, rest = _split_netloc(hostpart)
    if "," in netloc:
        raise ValueError(
            "Jumphost tunneling supports a single host only — this URI has "
            f"multiple seed hosts: {netloc!r}"
        )

    host, colon, port_str = netloc.rpartition(":")
    if not colon:
        raise ValueError(
            f"URI must include an explicit port to tunnel through a jumphost: {uri!r}"
        )
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(f"Could not parse port from URI: {netloc!r}") from exc

    return scheme, prefix, host, port, rest


def parse_uri_host(uri: str) -> tuple[str, int]:
    """Return ``(host, port)`` for a connection URI.

    Used to learn what a jumphost forward must point *at* before the local
    forwarded port is even known. See :func:`retarget_uri_host` for the full
    set of rejections (``+srv``, multi-host, missing port).
    """
    _scheme, _prefix, host, port, _rest = _parse_uri_authority(uri)
    return host, port


def retarget_uri_host(uri: str, new_host: str, new_port: int) -> tuple[str, str, int]:
    """Rewrite a connection URI's host:port — e.g. to point at a local SSH tunnel.

    Splits by hand (same style as :func:`encode_uri_credentials`) so userinfo,
    path, query, and fragment are never touched — only the ``host:port``
    immediately after ``scheme://[user:pass@]`` is replaced.

    Returns ``(rewritten_uri, original_host, original_port)`` — the originals
    are what the tunnel must actually forward to. Raises ``ValueError`` under
    the same conditions as :func:`parse_uri_host`.
    """
    scheme, prefix, host, port, rest = _parse_uri_authority(uri)
    rewritten = f"{scheme}://{prefix}{new_host}:{new_port}{rest}"
    return rewritten, host, port
