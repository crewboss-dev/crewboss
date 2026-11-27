"""Speech-to-text for the CA drawer's microphone (task #727): a SWAPPABLE provider, chosen in instance.yaml.

    speech:
      provider: aws_transcribe      # the only provider so far; unset/empty = no microphone anywhere
      region: ap-southeast-2
      language: en-AU               # a BCP-47 code the provider accepts (aws_transcribe: en-AU, en-US, en-GB, ...)

The browser streams its audio straight to the provider (it never passes through this server); the server only hands the
signed-in person a SHORT-LIVED signed address to connect to (aws_transcribe: a SigV4-presigned Transcribe streaming websocket,
valid 300 s at connect time), rate-limited per person. The provider's credentials are a 0600 file (config.SPEECH_CREDENTIALS_FILE):
for aws_transcribe, two lines `access_key_id=...` and `secret_access_key=...` (an IAM user allowed ONLY
transcribe:StartStreamTranscriptionWebSocket). They are read at use time and never logged or sent to the browser; the presigned
address carries the key id and a signature, never the secret.
With no provider, or no readable credentials, every function here says "not configured" and the routes answer 404."""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import logging
import threading
import time
from urllib.parse import quote

from app import config

log = logging.getLogger("crewboss.speech")

PROVIDERS = ("aws_transcribe",)
LISTEN_EXPIRES = 300
RATE = 16000
LANGS = frozenset({"en-AU", "en-US", "en-GB", "en-IN", "en-IE", "en-NZ", "en-ZA", "en-CA"})
LISTEN_BURST = 6
LISTEN_PER_SEC = 6 / 60.0


class NotConfigured(Exception):
    """No provider, an unknown one, or no readable credentials: the mic stays hidden."""


def provider() -> str | None:
    return config.SPEECH_PROVIDER if config.SPEECH_PROVIDER in PROVIDERS else None


def _credentials() -> dict:
    try:
        with open(config.SPEECH_CREDENTIALS_FILE, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        raise NotConfigured("no credentials file") from None
    kv = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    if not kv.get("access_key_id") or not kv.get("secret_access_key"):
        raise NotConfigured("credentials file lacks access_key_id / secret_access_key")
    return {"key_id": kv["access_key_id"], "secret": kv["secret_access_key"], "token": kv.get("session_token", "")}


def configured() -> bool:
    """True only when a known provider is set AND its credentials can be read (what decides if the drawer shows a mic)."""
    if not provider():
        return False
    try:
        _credentials()
        return True
    except NotConfigured:
        return False


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign_transcribe(creds: dict, region_name: str, rate: int, lang: str, now: _dt.datetime | None = None,
                       expires: int = LISTEN_EXPIRES) -> str:
    """SigV4 query-string presign of Transcribe streaming's websocket endpoint (service `transcribe`, host :8443, empty-payload
    hash, SignedHeaders=host). Returns the wss:// URL."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    host = f"transcribestreaming.{region_name}.amazonaws.com:8443"
    path = "/stream-transcription-websocket"
    scope = f"{day}/{region_name}/transcribe/aws4_request"
    params = {
        "language-code": lang, "media-encoding": "pcm", "sample-rate": str(rate),
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{creds['key_id']}/{scope}",
        "X-Amz-Date": amz_date, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host",
    }
    if creds.get("token"):
        params["X-Amz-Security-Token"] = creds["token"]
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    canonical = "\n".join(["GET", path, query, f"host:{host}\n", "host", hashlib.sha256(b"").hexdigest()])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(("AWS4" + creds["secret"]).encode(), day)
    for part in (region_name, "transcribe", "aws4_request"):
        k = _hmac(k, part)
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"wss://{host}{path}?{query}&X-Amz-Signature={sig}"


def listen_url(lang: str = "") -> dict:
    """{provider, url, expires_in, rate, language} for the configured provider, or NotConfigured."""
    if not provider():
        raise NotConfigured("no speech provider")
    lang = lang if lang in LANGS else (config.SPEECH_LANGUAGE if config.SPEECH_LANGUAGE in LANGS else "en-US")
    creds = _credentials()
    return {"provider": "aws_transcribe", "url": presign_transcribe(creds, config.SPEECH_REGION, RATE, lang),
            "expires_in": LISTEN_EXPIRES, "rate": RATE, "language": lang}


# --- rate limit: a token bucket per person (in-process, like the rest of the CA's live state) -------------------------
_buckets: dict[int, tuple[float, float]] = {}
_lock = threading.Lock()


def allow(user_id: int, now: float | None = None) -> float:
    """0.0 if the person may get an address now (a token is spent), else the seconds to wait."""
    now = time.monotonic() if now is None else now
    with _lock:
        tokens, last = _buckets.get(user_id, (float(LISTEN_BURST), now))
        tokens = min(float(LISTEN_BURST), tokens + (now - last) * LISTEN_PER_SEC)
        if tokens >= 1:
            _buckets[user_id] = (tokens - 1, now)
            return 0.0
        _buckets[user_id] = (tokens, now)
        return (1 - tokens) / LISTEN_PER_SEC
