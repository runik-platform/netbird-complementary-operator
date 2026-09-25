import responses

from webhook.netbird_client import NetBirdClient, NetBirdError, NotFound, Resolver


@responses.activate
def test_get_sets_auth_and_returns_json():
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "admins"}],
        status=200,
    )
    c = NetBirdClient()
    out = c.get("/groups")
    assert out == [{"id": "g1", "name": "admins"}]
    assert responses.calls[0].request.headers["Authorization"] == "Token test-pat"


@responses.activate
def test_404_raises_notfound():
    responses.get("https://nb.test/api/routes/missing", status=404, body="gone")
    c = NetBirdClient()
    try:
        c.get("/routes/missing")
    except NotFound as e:
        assert e.status == 404
    else:
        raise AssertionError("expected NotFound")


@responses.activate
def test_retries_on_429_then_succeeds():
    responses.get("https://nb.test/api/groups", status=429, body="slow down")
    responses.get("https://nb.test/api/groups", json=[], status=200)
    c = NetBirdClient()
    assert c.get("/groups") == []
    assert len(responses.calls) == 2


@responses.activate
def test_retries_on_5xx_then_gives_up():
    for _ in range(5):
        responses.get("https://nb.test/api/groups", status=503, body="boom")
    c = NetBirdClient(max_retries=2)
    try:
        c.get("/groups")
    except NetBirdError as e:
        assert "503" in str(e)
    else:
        raise AssertionError("expected error")


@responses.activate
def test_resolver_group_id_exact_match():
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "admins"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "admins"})],
    )
    r = Resolver(NetBirdClient())
    assert r.group_id("admins") == "g1"


@responses.activate
def test_resolver_group_ids_cached_per_name():
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "admins"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "admins"})],
    )
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g2", "name": "devs"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "devs"})],
    )
    r = Resolver(NetBirdClient())
    assert r.group_ids(["admins", "devs", "admins"]) == ["g1", "g2", "g1"]
    # Third lookup is cached.
    assert len(responses.calls) == 2


@responses.activate
def test_resolver_group_id_missing_raises():
    responses.get(
        "https://nb.test/api/groups",
        json=[],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "ghost"})],
    )
    r = Resolver(NetBirdClient())
    try:
        r.group_id("ghost")
    except KeyError as e:
        assert "ghost" in str(e)
    else:
        raise AssertionError("expected KeyError")


@responses.activate
def test_api_key_file_is_hot_reloaded_for_each_request(tmp_path, monkeypatch):
    token_file = tmp_path / "api-key"
    token_file.write_text("first-token\n")
    monkeypatch.setenv("NB_API_KEY_FILE", str(token_file))
    monkeypatch.setenv("NB_API_KEY", "environment-fallback")
    responses.get("https://nb.test/api/groups", json=[], status=200)
    responses.get("https://nb.test/api/users", json=[], status=200)
    client = NetBirdClient()

    client.get("/groups")
    token_file.write_text("second-token\n")
    client.get("/users")

    assert responses.calls[0].request.headers["Authorization"] == "Token first-token"
    assert responses.calls[1].request.headers["Authorization"] == "Token second-token"


@responses.activate
def test_explicit_token_precedes_file_and_environment(tmp_path, monkeypatch):
    token_file = tmp_path / "api-key"
    token_file.write_text("file-token")
    monkeypatch.setenv("NB_API_KEY_FILE", str(token_file))
    monkeypatch.setenv("NB_API_KEY", "environment-token")
    responses.get("https://nb.test/api/groups", json=[], status=200)
    NetBirdClient(token="explicit-token").get("/groups")
    assert responses.calls[0].request.headers["Authorization"] == "Token explicit-token"


@responses.activate
def test_environment_api_key_remains_compatible(monkeypatch):
    monkeypatch.delenv("NB_API_KEY_FILE", raising=False)
    monkeypatch.setenv("NB_API_KEY", "legacy-token")
    responses.get("https://nb.test/api/groups", json=[], status=200)
    NetBirdClient().get("/groups")
    assert responses.calls[0].request.headers["Authorization"] == "Token legacy-token"
