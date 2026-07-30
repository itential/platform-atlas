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

__all__ = ["encode_uri_credentials"]


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
