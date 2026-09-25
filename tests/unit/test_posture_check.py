import json

import responses

from webhook.handlers import posture_check
from webhook.netbird_client import NetBirdClient, Resolver


@responses.activate
def test_creates_translates_camel_to_snake():
    responses.get("https://nb.test/api/posture-checks", json=[], status=200)
    responses.post("https://nb.test/api/posture-checks", json={"id": "pc1"}, status=200)
    parent = {
        "spec": {
            "name": "min-version",
            "description": "require nb 0.68",
            "checks": {
                "nbVersionCheck": {"minVersion": "0.68.0"},
                "osVersionCheck": {
                    "linux": {"minKernelVersion": "5.15"},
                    "darwin": {"minVersion": "14.0"},
                },
                "geoLocationCheck": {
                    "action": "allow",
                    "locations": [{"countryCode": "AR"}, {"countryCode": "UY", "cityName": "Montevideo"}],
                },
                "processCheck": {"processes": [{"linuxPath": "/usr/bin/netbird"}]},
            },
        },
        "status": {},
    }
    out = posture_check.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "pc1"}
    body = json.loads(next(c for c in responses.calls if c.request.method == "POST").request.body)
    assert body["name"] == "min-version"
    assert body["checks"]["nb_version_check"] == {"min_version": "0.68.0"}
    assert body["checks"]["os_version_check"]["linux"] == {"min_kernel_version": "5.15"}
    assert body["checks"]["os_version_check"]["darwin"] == {"min_version": "14.0"}
    assert body["checks"]["geo_location_check"]["action"] == "allow"
    assert body["checks"]["geo_location_check"]["locations"] == [
        {"country_code": "AR"},
        {"country_code": "UY", "city_name": "Montevideo"},
    ]
    assert body["checks"]["process_check"] == {"processes": [{"linux_path": "/usr/bin/netbird"}]}


@responses.activate
def test_noop_when_server_returns_null_or_empty_for_missing_checks():
    """Server-side response may contain null / {} for checks we didn't set.
    With normalization, that should be treated as equal (no PUT)."""
    responses.get(
        "https://nb.test/api/posture-checks/pc1",
        json={
            "id": "pc1",
            "name": "min",
            "description": "d",
            "checks": {
                "nb_version_check": {"min_version": "0.68.0"},
                "os_version_check": None,
                "geo_location_check": {},
                "process_check": {"processes": []},
            },
        },
        status=200,
    )
    parent = {
        "spec": {
            "name": "min",
            "description": "d",
            "checks": {"nbVersionCheck": {"minVersion": "0.68.0"}},
        },
        "status": {"id": "pc1"},
    }
    posture_check.sync(parent, Resolver(NetBirdClient()))
    assert not any(c.request.method == "PUT" for c in responses.calls)
