"""HTTP client for the LHDN MyInvois e-Invoice API (Platform + e-Invoicing APIs).

Endpoints implemented (SDK v1.0):
  POST /connect/token                                   - Login as taxpayer / intermediary
  GET  /api/v1.0/taxpayer/validate/{tin}                 - Validate taxpayer TIN
  GET  /api/v1.0/taxpayer/search/tin                     - Search taxpayer TIN
  GET  /api/v1.0/documenttypes                           - Document types
  POST /api/v1.0/documentsubmissions                     - Submit documents
  GET  /api/v1.0/documentsubmissions/{submissionUid}     - Get submission
  GET  /api/v1.0/documents/{uuid}/raw                    - Get document
  GET  /api/v1.0/documents/{uuid}/details                - Get document details (validation results)
  PUT  /api/v1.0/documents/state/{uuid}/state            - Cancel (issuer) / Reject (buyer) document
  GET  /api/v1.0/documents/recent                        - Recent documents (sent / received)
  GET  /api/v1.0/documents/search                        - Search documents
"""
import json
import time

import requests

BASE_URLS = {
    "sandbox": "https://preprod-api.myinvois.hasil.gov.my",
    "production": "https://api.myinvois.hasil.gov.my",
}
PORTAL_URLS = {
    "sandbox": "https://preprod.myinvois.hasil.gov.my",
    "production": "https://myinvois.hasil.gov.my",
}


class LHDNError(Exception):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


class LHDNClient:
    def __init__(self, env, client_id, client_secret, onbehalfof=None, logger=None, timeout=30):
        if env not in BASE_URLS:
            raise ValueError(f"Unknown LHDN environment '{env}'")
        self.env = env
        self.base = BASE_URLS[env]
        self.portal = PORTAL_URLS[env]
        self.client_id = client_id
        self.client_secret = client_secret
        self.onbehalfof = onbehalfof or None
        self.logger = logger
        self.timeout = timeout
        self._token = None
        self._token_exp = 0

    # ------------------------------------------------------------ plumbing
    def _log(self, method, url, status, req, resp):
        if self.logger:
            self.logger(self.env, method, url, status, req, resp)

    def token(self):
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        url = f"{self.base}/connect/token"
        data = {"client_id": self.client_id, "client_secret": self.client_secret,
                "grant_type": "client_credentials", "scope": "InvoicingAPI"}
        headers = {"onbehalfof": self.onbehalfof} if self.onbehalfof else {}
        r = requests.post(url, data=data, headers=headers, timeout=self.timeout)
        self._log("POST", url, r.status_code, {"client_id": self.client_id, "onbehalfof": self.onbehalfof},
                  r.text[:2000])
        if r.status_code != 200:
            raise LHDNError(f"Login failed ({r.status_code}): {r.text[:300]}", r.status_code, r.text)
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _request(self, method, path, params=None, body=None, retry=True):
        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.token()}", "Accept": "application/json",
                   "Accept-Language": "en"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = requests.request(method, url, params=params, data=json.dumps(body) if body is not None else None,
                             headers=headers, timeout=self.timeout)
        log_body = body
        if body and "documents" in body:   # don't bloat the log with base64 documents
            log_body = {"documents": [{k: (v if k != "document" else f"<{len(v)} b64 chars>")
                                       for k, v in d.items()} for d in body["documents"]]}
        self._log(method, url + (f"?{requests.compat.urlencode(params)}" if params else ""), r.status_code,
                  log_body, r.text[:5000])
        if r.status_code == 401 and retry:
            self._token = None
            return self._request(method, path, params, body, retry=False)
        if r.status_code == 429 and retry:
            time.sleep(min(int(r.headers.get("Retry-After", "2")), 10))
            return self._request(method, path, params, body, retry=False)
        if r.status_code >= 400:
            raise LHDNError(f"LHDN API {method} {path} failed ({r.status_code}): {r.text[:500]}",
                            r.status_code, r.text)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {"raw": r.text}

    # ------------------------------------------------------------ API
    def validate_tin(self, tin, id_type, id_value):
        try:
            self._request("GET", f"/api/v1.0/taxpayer/validate/{tin}",
                          params={"idType": id_type, "idValue": id_value})
            return True
        except LHDNError as e:
            if e.status in (400, 404):
                return False
            raise

    def search_tin(self, id_type=None, id_value=None, taxpayer_name=None):
        params = {k: v for k, v in {"idType": id_type, "idValue": id_value,
                                    "taxpayerName": taxpayer_name}.items() if v}
        return self._request("GET", "/api/v1.0/taxpayer/search/tin", params=params)

    def document_types(self):
        return self._request("GET", "/api/v1.0/documenttypes")

    def submit_documents(self, documents):
        """documents: list of {"format","document","documentHash","codeNumber"}"""
        return self._request("POST", "/api/v1.0/documentsubmissions", body={"documents": documents})

    def get_submission(self, submission_uid):
        return self._request("GET", f"/api/v1.0/documentsubmissions/{submission_uid}",
                             params={"pageNo": 1, "pageSize": 100})

    def get_document(self, uuid):
        return self._request("GET", f"/api/v1.0/documents/{uuid}/raw")

    def get_document_details(self, uuid):
        return self._request("GET", f"/api/v1.0/documents/{uuid}/details")

    def cancel_document(self, uuid, reason):
        return self._request("PUT", f"/api/v1.0/documents/state/{uuid}/state",
                             body={"status": "cancelled", "reason": reason})

    def reject_document(self, uuid, reason):
        return self._request("PUT", f"/api/v1.0/documents/state/{uuid}/state",
                             body={"status": "rejected", "reason": reason})

    def recent_documents(self, direction="Received", page=1, page_size=50):
        return self._request("GET", "/api/v1.0/documents/recent",
                             params={"InvoiceDirection": direction, "pageNo": page, "pageSize": page_size})

    def search_documents(self, **params):
        return self._request("GET", "/api/v1.0/documents/search", params=params)

    def validation_url(self, uuid, long_id):
        """Public validation link that must be printed as a QR code on the visual invoice."""
        return f"{self.portal}/{uuid}/share/{long_id}"
