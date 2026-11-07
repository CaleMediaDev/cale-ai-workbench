"""Direct eBay read-only order intake and durable print preparation queue.

Uses Fulfillment API v1 getOrders/getOrder and the seller's OAuth refresh token.
No shipping, listing, refund or printer mutations. Credentials are supplied by
the private runtime; this module never saves or logs them or buyer details.
"""

import base64
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SCOPE = "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly"
BASE = "https://api.ebay.com"


class IntakeError(RuntimeError):
    pass


class OrderHold(IntakeError):
    pass


def stamp(value):
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise IntakeError("UTC timestamp required")
    return d.astimezone(timezone.utc)


def iso(value):
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise IntakeError("Unexpected eBay redirect; credentials were not forwarded")


class EbayClient:
    def __init__(self, client_id, client_secret, refresh_token, opener=None):
        if not all((client_id, client_secret, refresh_token)):
            raise IntakeError(
                "eBay developer keys and seller authorization are required"
            )
        self.credentials = (client_id, client_secret, refresh_token)
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.token = None
        self.expires = 0

    def request(self, request):
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise IntakeError("eBay response too large")
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Never echo a response body containing credentials or buyer data.
            raise IntakeError("eBay HTTP " + str(exc.code)) from None

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        client_id, secret, refresh = self.credentials
        basic = base64.b64encode((client_id + ":" + secret).encode()).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "refresh_token", "refresh_token": refresh, "scope": SCOPE}
        ).encode()
        result = self.request(
            urllib.request.Request(
                BASE + "/identity/v1/oauth2/token",
                data=body,
                headers={
                    "Authorization": "Basic " + basic,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        )
        if not result.get("access_token") or int(result.get("expires_in", 0)) < 120:
            raise IntakeError("eBay did not return a usable access token")
        self.token = result["access_token"]
        self.expires = time.time() + int(result["expires_in"]) - 60
        return self.token

    def get(self, path):
        if not path.startswith("/sell/fulfillment/v1/order") or "://" in path:
            raise IntakeError("Unexpected API path")
        return self.request(
            urllib.request.Request(
                BASE + path,
                headers={
                    "Authorization": "Bearer " + self.access_token(),
                    "Accept": "application/json",
                },
            )
        )

    def order(self, order_id):
        return self.get(
            "/sell/fulfillment/v1/order/" + urllib.parse.quote(order_id, safe="")
        )

    def orders(self, since, until):
        """Bounded modified-date window; include shipped/cancelled updates too."""
        found = []
        offset = 0
        for _ in range(100):
            query = urllib.parse.urlencode(
                {
                    "filter": f"lastmodifieddate:[{iso(since)}..{iso(until)}]",
                    "limit": 200,
                    "offset": offset,
                }
            )
            data = self.get("/sell/fulfillment/v1/order?" + query)
            page = data.get("orders", [])
            if not isinstance(page, list):
                raise IntakeError("Unexpected eBay order page")
            found.extend(page)
            if type(data.get("total")) is not int or data["total"] < 0:
                raise IntakeError("Missing eBay pagination total")
            total = data["total"]
            offset += len(page)
            if offset >= total:
                return found
            if not page:
                raise IntakeError("eBay pagination stopped early")
        raise IntakeError("Too many orders for one poll; checkpoint was not advanced")


def normalized(order, line):
    """Whitelist production data; do not persist customer identity or addresses."""
    return {
        "order_id": str(order.get("orderId", "")),
        "line_id": str(line.get("lineItemId", "")),
        "created": order.get("creationDate", ""),
        "modified": order.get("lastModifiedDate", ""),
        "payment": order.get("orderPaymentStatus"),
        "order_fulfillment": order.get("orderFulfillmentStatus"),
        "cancel_state": order.get("cancelStatus", {}).get("cancelState"),
        "order_refund": bool(order.get("paymentSummary", {}).get("refunds")),
        "line_refund": bool(line.get("refunds")),
        "fulfillment": line.get("lineItemFulfillmentStatus"),
        "sku": str(line.get("sku", "")),
        "item_id": str(line.get("legacyItemId", "")),
        "variation_id": str(line.get("legacyVariationId", "")),
        "aspects": line.get("variationAspects", []),
        "quantity": line.get("quantity"),
        "title": str(line.get("title", ""))[:300],
    }


def fingerprint(row, mapping):
    fields = ("sku", "item_id", "variation_id", "aspects", "quantity")
    return hashlib.sha256(
        json.dumps(
            {"item": {k: row[k] for k in fields}, "mapping": mapping},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def match(row, mappings):
    by_sku = [m for m in mappings if row["sku"] and m.get("sku") == row["sku"]]
    by_listing = [
        m
        for m in mappings
        if not m.get("sku")
        and m.get("item_id") == row["item_id"]
        and m.get("variation_id", "") == row["variation_id"]
    ]
    candidates = by_sku or by_listing
    # Never guess between different listings, colors, sizes or multiple mappings.
    return candidates[0] if len(candidates) == 1 else None


def hold_reason(row, mapping, cutoff):
    try:
        if stamp(row["created"]) < cutoff:
            return "Created before automatic-intake activation"
    except (ValueError, TypeError):
        return "Missing or invalid creation time"
    if row["payment"] != "PAID":
        return "Payment is not fully paid"
    if row["cancel_state"] != "NONE_REQUESTED":
        return "Cancellation needs review"
    if row["order_refund"] or row["line_refund"]:
        return "Refund needs review"
    if row["order_fulfillment"] not in ("NOT_STARTED", "IN_PROGRESS"):
        return "Order already fulfilled or status unknown"
    if row["fulfillment"] != "NOT_STARTED":
        return "Line is already fulfilled, partly fulfilled or unknown"
    if type(row["quantity"]) is not int or not 1 <= row["quantity"] <= 50:
        return "Quantity needs review"
    if not mapping:
        return "No unique listing or SKU match"
    if mapping.get("approved") is not True:
        return "Product mapping has not been approved"
    if not mapping.get("model_id") or not mapping.get("recipe_id"):
        return "Missing model or print recipe"
    if mapping.get("material") != "PLA" or mapping.get("color") not in (
        "black",
        "white",
    ):
        return "Material or color needs review"
    units = mapping.get("units_per_sale")
    if type(units) is not int or not 1 <= units <= 50 or units * row["quantity"] > 200:
        return "Pack quantity needs review"
    # Variations require an exact approved variation ID even when a SKU matches.
    if row["variation_id"] and mapping.get("variation_id") != row["variation_id"]:
        return "Variation needs review"
    if row["aspects"] and not row["variation_id"]:
        return (
            "Variation aspects supplied without a stable variation ID; review required"
        )
    return None
