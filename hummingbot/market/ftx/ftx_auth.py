import time
import hmac
import hashlib
import urllib
from typing import Dict, Any, Tuple
from requests import Request

import ujson


class FtxAuth:
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key

    def generate_auth_dict(
        self,
        http_method: str,
        url: str,
        params: Dict[str, Any] = None,
        body: Dict[str, Any] = None,
    ) -> Dict[str, any]:

        ts = int(time.time() * 1000)
        if http_method == "GET":
            req_body = ""
        else:
            req_body = json.dumps(body)
        request = Request(http_method, url)
        prepared = request.prepare()
        content_to_sign = "".join([str(ts), prepared.method, prepared.path_url, req_body])
        signature = hmac.new(self.secret_key.encode(), content_to_sign.encode(), hashlib.sha256).hexdigest()

        # V3 Authentication headers
        headers = {
            "FTX-KEY": self.api_key,
            "FTX-SIGN": signature,
            "FTX-TS": str(ts)
        }

        return headers

    def generate_websocket_subscription(self):
        ts = int(1000*time.time())
        presign = f"{ts}websocket_login"
        sign = hmac.new(self.secret_key.encode(),presign.encode(),'sha256').hexdigest()
        subscribe = {
          "args": {
            "key": self.api_key,
            "sign": sign,
            "time": ts,  
          },
          "op": "login"
        }
        return subscribe
