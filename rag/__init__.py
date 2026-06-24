import os

_PROXY = "http://127.0.0.1:3128"
os.environ.setdefault("HTTP_PROXY", _PROXY)
os.environ.setdefault("HTTPS_PROXY", _PROXY)
