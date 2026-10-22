# API

The REST API lives under `/api/v1/`. The OpenAPI 3.1 description is at
`/api/schema/` (YAML, or `?format=json`); load it into any OpenAPI viewer or
client generator.

## Authentication

* **Personal token**: *API tokens* in the user menu. Choose read and/or write
  access, an expiry and optionally one tenant. Send it as
  `Authorization: Bearer pmat_...`. The token acts with your permissions.
* **OAuth 2.0 client credentials** for services: an administrator creates a
  client under *Administration → API clients* and gives its service user roles.
  Then:

```bash
curl -u "$CLIENT_ID:$CLIENT_SECRET" -d grant_type=client_credentials \
  https://patchmanager.example.com/api/oauth/token/
```

The answer contains an `access_token` valid for one hour.

Tokens and clients can be **limited** further: to some kinds of objects (for example
only devices and IP addresses) and to one location (a site, building, floor or room
and everything in it). A limited token never does more, even if its user is a super
admin; read only tokens are limited to reading in every check.

## Conventions

* JSON in and out; lists are paginated with `?limit=` (default 100, at most 1000)
  and `?offset=`, and return `count`, `next`, `previous` and `results`.
* Filters per list, for example `/api/v1/devices/?room=<id>&status=active`,
  `/api/v1/vlans/?vid=120`, `?q=` searches names.
* Create with `POST`, change with `PATCH` (only sent fields change), delete with
  `DELETE`. Mount or move a device with `POST /api/v1/devices/<id>/place/`.
* Errors: `400` with field messages or `{"detail": ...}`, `401` without a valid
  token, `403` without permission or scope, `404` for objects you cannot see,
  `429` when throttled.
* IP address management: `/api/v1/vrfs/`, `/api/v1/prefixes/` and
  `/api/v1/ip-addresses/`. `?within=10.0.0.0/16` lists prefixes or addresses inside
  a network, `?q=` searches the text of the network or address. Addresses take a
  `port` id to sit on a port.
* Configurations: `GET`/`POST /api/v1/devices/<id>/configs/` lists or stores versions
  (`{"content": "...", "comment": "..."}`; `201` new, `200` unchanged),
  `GET /api/v1/devices/<id>/configs/<version>/` returns one with its text.

## Webhooks

*Administration → Webhooks* sends every change of the current tenant as a JSON
POST to your URL. Choose events with patterns such as `dcim.device.*`,
`cabling.cable.insert` or `*`. Each request carries

* `PatchManager-Event`: for example `dcim.rack.update`
* `PatchManager-Delivery`: a unique id (deliveries can repeat; deduplicate on it)
* `PatchManager-Signature`: `t=<unix time>,v1=<signature>`

Verify the signature before trusting the body:

```python
import hashlib, hmac, time


def valid(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(","))
    expected = hmac.new(
        secret.encode(), f"{parts['t']}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return (
        hmac.compare_digest(expected, parts["v1"])
        and abs(time.time() - int(parts["t"])) < tolerance
    )
```

Answer with any 2xx status. Other answers and network errors are retried after
1 minute, 5 minutes, 30 minutes, 2, 6 and 24 hours. Only public addresses are
reached unless the operator allows internal networks.
