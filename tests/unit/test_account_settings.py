import json

import responses

from webhook.handlers import account_settings
from webhook.netbird_client import NetBirdClient, Resolver


PARENT = {
    "apiVersion": "netbird.io/v1alpha1",
    "kind": "NetBirdAccountSettings",
    "metadata": {"name": "default", "generation": 1},
    "spec": {
        "peerLoginExpiration": {"enabled": True, "seconds": 86400},
        "regularUsersViewBlocked": True,
        "extra": {"userApprovalRequired": False},
    },
}


@responses.activate
def test_sync_fetches_account_and_puts_settings():
    responses.get(
        "https://nb.test/api/accounts",
        json=[{
            "id": "acc-1",
            "settings": {
                "peer_login_expiration_enabled": False,
                "peer_login_expiration": 3600,
                "peer_inactivity_expiration_enabled": True,
                "peer_inactivity_expiration": 7200,
                "groups_propagation_enabled": False,
                "future_setting": "preserved",
                "extra": {
                    "peer_approval_enabled": True,
                    "user_approval_required": True,
                    "network_traffic_logs_enabled": True,
                    "network_traffic_logs_groups": ["g1"],
                    "network_traffic_packet_counter_enabled": True,
                },
            },
        }],
        status=200,
    )
    responses.put("https://nb.test/api/accounts/acc-1", json={"id": "acc-1"}, status=200)

    r = Resolver(NetBirdClient())
    status = account_settings.sync(PARENT, r)
    assert status == {"id": "acc-1"}

    put_call = responses.calls[1].request
    body = json.loads(put_call.body)
    assert body["settings"]["peer_login_expiration_enabled"] is True
    assert body["settings"]["peer_login_expiration"] == 86400
    assert body["settings"]["regular_users_view_blocked"] is True
    assert body["settings"]["peer_inactivity_expiration_enabled"] is True
    assert body["settings"]["peer_inactivity_expiration"] == 7200
    assert body["settings"]["groups_propagation_enabled"] is False
    assert body["settings"]["future_setting"] == "preserved"
    extra = body["settings"]["extra"]
    assert extra == {
        "peer_approval_enabled": True,
        "user_approval_required": False,
        "network_traffic_logs_enabled": True,
        "network_traffic_logs_groups": ["g1"],
        "network_traffic_packet_counter_enabled": True,
    }


@responses.activate
def test_sparse_spec_preserves_required_live_account_settings():
    current = {
        "peer_login_expiration_enabled": True,
        "peer_login_expiration": 86400,
        "jwt_groups_enabled": False,
        "jwt_groups_claim_name": "",
        "jwt_allow_groups": [],
    }
    responses.get(
        "https://nb.test/api/accounts",
        json=[{"id": "acc-1", "settings": current}],
        status=200,
    )
    responses.put("https://nb.test/api/accounts/acc-1", json={"id": "acc-1"}, status=200)

    parent = {
        "metadata": {"name": "default", "generation": 1},
        "spec": {
            "groupsPropagationEnabled": True,
            "jwtGroups": {"enabled": True, "claimName": "groups"},
        },
    }
    account_settings.sync(parent, Resolver(NetBirdClient()))

    body = json.loads(responses.calls[1].request.body)
    assert body["settings"] == {
        **current,
        "groups_propagation_enabled": True,
        "jwt_groups_enabled": True,
        "jwt_groups_claim_name": "groups",
    }


@responses.activate
def test_sync_skips_put_when_owned_settings_are_already_current():
    current = {
        "groups_propagation_enabled": True,
        "routing_peer_dns_resolution_enabled": True,
    }
    responses.get(
        "https://nb.test/api/accounts",
        json=[{"id": "acc-1", "settings": current}],
        status=200,
    )

    status = account_settings.sync(
        {
            "spec": {
                "groupsPropagationEnabled": True,
                "routingPeerDnsResolutionEnabled": True,
            }
        },
        Resolver(NetBirdClient()),
    )

    assert status == {"id": "acc-1"}
    assert [call.request.method for call in responses.calls] == ["GET"]


@responses.activate
def test_finalize_is_noop():
    r = Resolver(NetBirdClient())
    assert account_settings.finalize(PARENT, r) == {}
    assert len(responses.calls) == 0
