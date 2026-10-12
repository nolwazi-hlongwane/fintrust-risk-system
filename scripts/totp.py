"""Time-based one-time passwords (TOTP, RFC 6238): the 6-digit codes an
authenticator app shows.

The FinTrust user pool requires MFA for everyone, including the automation user
that the verification scripts and the end-to-end test sign in as. Those scripts
work out the current code from the automation user's secret, exactly as an
authenticator app does on a phone:

    code = the last 6 digits of HMAC-SHA1(secret, number of 30-second steps since 1970)

Standard library only.
"""

import base64
import hashlib
import hmac
import struct
import time

STEP_SECONDS = 30
DIGITS = 6


def _key(secret):
    """The secret as bytes. Cognito gives it as base32 text, without padding."""
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def code_at(secret, step, digits=DIGITS):
    """The code for one 30-second step."""
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F                                      # "dynamic truncation", RFC 4226
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10 ** digits).zfill(digits)


def current_step(now=None):
    return int((time.time() if now is None else now) // STEP_SECONDS)


def fresh_code(secret, used_step=None, now=None, sleep=time.sleep):
    """A code for the current step, waiting for the next step if `used_step`
    has already been used: Cognito accepts each code only once."""
    step = current_step(now)
    if used_step is not None and step <= used_step:
        moment = time.time() if now is None else now
        sleep((used_step + 1) * STEP_SECONDS - moment + 0.5)
        step = used_step + 1
    return code_at(secret, step), step
