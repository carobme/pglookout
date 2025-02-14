# Copied from https://github.com/ohmu/ohmu_common_py ohmu_common_py/pgutil.py version 0.0.1-0-unknown-fa54b44
"""
pglookout - postgresql utility functions

Copyright (c) 2015 Ohmu Ltd
See LICENSE for details
"""
from urllib.parse import urlparse, parse_qs  # pylint: disable=no-name-in-module, import-error
import psycopg2.extensions


def create_connection_string(connection_info):
    return psycopg2.extensions.make_dsn(**connection_info)


def mask_connection_info(info):
    masked_info = get_connection_info(info)
    password = masked_info.pop("password", None)
    connection_string = create_connection_string(masked_info)
    message = "no password" if password is None else "hidden password"
    return f"{connection_string}; {message}"


def get_connection_info_from_config_line(line):
    _, value = line.split("=", 1)
    value = value.strip()[1:-1].replace("''", "'")
    return get_connection_info(value)


def get_connection_info(info):
    """turn a connection info object into a dict or return it if it was a
    dict already.  supports both the traditional libpq format and the new
    url format"""
    if isinstance(info, dict):
        return info.copy()
    if info.startswith("postgres://") or info.startswith("postgresql://"):
        return parse_connection_string_url(info)
    return parse_connection_string_libpq(info)


def parse_connection_string_url(url):
    # drop scheme from the url as some versions of urlparse don't handle
    # query and path properly for urls with a non-http scheme
    schemeless_url = url.split(":", 1)[1]
    p = urlparse(schemeless_url)
    fields = {}
    if p.hostname:
        fields["host"] = p.hostname
    if p.port:
        fields["port"] = str(p.port)
    if p.username:
        fields["user"] = p.username
    if p.password is not None:
        fields["password"] = p.password
    if p.path and p.path != "/":
        fields["dbname"] = p.path[1:]
    for k, v in parse_qs(p.query).items():
        fields[k] = v[-1]
    return fields


def parse_connection_string_libpq(connection_string):
    """parse a postgresql connection string as defined in
    http://www.postgresql.org/docs/current/static/libpq-connect.html#LIBPQ-CONNSTRING"""
    fields = {}
    while True:
        connection_string = connection_string.strip()
        if not connection_string:
            break
        if "=" not in connection_string:
            raise ValueError(f"expecting key=value format in connection_string fragment {connection_string!r}")
        key, rem = connection_string.split("=", 1)
        if rem.startswith("'"):
            asis, value = False, ""
            for i in range(1, len(rem)):
                if asis:
                    value += rem[i]
                    asis = False
                elif rem[i] == "'":
                    break  # end of entry
                elif rem[i] == "\\":
                    asis = True
                else:
                    value += rem[i]
            else:
                raise ValueError(f"invalid connection_string fragment {rem!r}")
            connection_string = rem[i + 1:]  # pylint: disable=undefined-loop-variable
        else:
            res = rem.split(None, 1)
            if len(res) > 1:
                value, connection_string = res
            else:
                value, connection_string = rem, ""
        fields[key] = value
    return fields
