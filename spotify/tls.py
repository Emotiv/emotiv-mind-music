"""The certificate authorities every Spotify HTTPS request is checked against.

Python's default context asks OpenSSL for its compiled-in CA file, and in the
macOS bundle that path belongs to the build machine: the release's libcrypto
looks for /Library/Frameworks/Python.framework/Versions/3.12/etc/openssl/
cert.pem, which exists only where python.org's Python 3.12 is installed. On
any other Mac there are no trusted roots, and every Spotify call fails with
CERTIFICATE_VERIFY_FAILED ("unable to get local issuer certificate") while
running from source works fine. certifi's bundle travels inside the app, so
the check no longer depends on what happens to be installed.
"""
import functools
import ssl

import certifi


@functools.lru_cache(maxsize=None)
def context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())
