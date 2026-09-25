from pathlib import Path

import yaml
from jsonschema import Draft7Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[2]


def load_crds(directory):
    return {
        path.name: yaml.safe_load(path.read_text())
        for path in sorted(directory.glob("*.yaml"))
    }


def test_all_source_and_chart_crds_are_valid_and_identical():
    source = load_crds(ROOT / "crds")
    chart = load_crds(ROOT / "chart" / "crds")
    assert len(source) == 18
    assert source == chart
    for crd in source.values():
        assert crd["apiVersion"] == "apiextensions.k8s.io/v1"
        assert crd["kind"] == "CustomResourceDefinition"
        version = crd["spec"]["versions"][0]
        assert version["served"] is True
        assert version["storage"] is True
        assert version["schema"]["openAPIV3Schema"]["type"] == "object"
        assert version["subresources"] == {"status": {}}


def test_role_binding_and_token_admission_contracts():
    crds = load_crds(ROOT / "crds")
    rb = crds["netbird-role-binding.yaml"]["spec"]["versions"][0]
    rb_spec = rb["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert rb_spec["properties"]["role"]["enum"] == ["admin"]
    assert rb_spec["properties"]["includeServiceUsers"]["default"] is False
    assert rb_spec["properties"]["pollIntervalSeconds"]["default"] == 60

    token = crds["netbird-token.yaml"]["spec"]["versions"][0]
    token_spec = token["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert token_spec["properties"]["expiresInDays"]["default"] == 90
    assert token_spec["properties"]["renewBeforeDays"]["minimum"] == 1
    rule = token_spec["x-kubernetes-validations"][0]["rule"]
    assert "self.renewBeforeDays < self.expiresInDays" in rule


def test_identity_sync_admission_contract():
    crd = load_crds(ROOT / "crds")["netbird-identity-sync.yaml"]
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert spec["properties"]["source"]["x-kubernetes-validations"][0]["rule"] == "self == oldSelf"
    assert spec["properties"]["instanceId"]["x-kubernetes-validations"][0]["rule"] == "self == oldSelf"
    # Kubernetes rejects uniqueItems on CRDs because validation is quadratic;
    # the glyph and reconciler both reject duplicate role names linearly.
    assert "uniqueItems" not in spec["properties"]["realmRoles"]
    assert spec["properties"]["pollIntervalSeconds"]["default"] == 60
    assert spec["properties"]["deletionPolicy"]["default"] == "Retain"


def test_instance_secret_reference_rules_compare_scalar_fields():
    crd = load_crds(ROOT / "crds")["netbird-instance.yaml"]
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    immutable_rule = spec["x-kubernetes-validations"][1]["rule"]
    bootstrap_rule = spec["properties"]["bootstrap"]["x-kubernetes-validations"][0]["rule"]
    assert "credentialSecretRef.name == oldSelf.bootstrap.credentialSecretRef.name" in immutable_rule
    assert "credentialSecretRef.key == oldSelf.bootstrap.credentialSecretRef.key" in immutable_rule
    assert "adoptionCredentialSecretRef.name != self.credentialSecretRef.name" in bootstrap_rule


def test_policy_and_networks_admission_contracts():
    crds = load_crds(ROOT / "crds")
    policy = crds["netbird-policy.yaml"]["spec"]["versions"][0]
    policy_spec = policy["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert policy_spec["properties"]["state"]["enum"] == ["present", "absent"]
    rule = policy_spec["properties"]["rules"]["items"]
    assert rule["properties"]["protocol"]["enum"] == ["all", "tcp", "udp", "icmp"]
    assert rule["properties"]["action"]["enum"] == ["accept"]
    assert rule["properties"]["ports"]["items"]["maximum"] == 65535
    assert rule["properties"]["portRanges"]["items"]["properties"]["end"]["maximum"] == 65535

    resource = crds["netbird-network-resource.yaml"]["spec"]["versions"][0]
    resource_spec = resource["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert resource_spec["properties"]["networkRef"]["required"] == ["name"]

    router = crds["netbird-network-router.yaml"]["spec"]["versions"][0]
    router_spec = router["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert len(router_spec["x-kubernetes-validations"]) == 2
    assert router_spec["properties"]["metric"]["maximum"] == 9999


def test_all_complementary_examples_validate_against_crd_openapi():
    crds = load_crds(ROOT / "crds")
    schemas = {
        crd["spec"]["names"]["kind"]: crd["spec"]["versions"][0]["schema"][
            "openAPIV3Schema"
        ]
        for crd in crds.values()
    }
    checked = []
    for directory in (ROOT / "examples", ROOT / "tests" / "e2e"):
        for path in sorted(directory.glob("*.yaml")):
            for document in yaml.safe_load_all(path.read_text()):
                if not document or document.get("kind") not in schemas:
                    continue
                errors = list(
                    Draft7Validator(
                        schemas[document["kind"]], format_checker=FormatChecker()
                    ).iter_errors(document)
                )
                assert not errors, f"{path}: {[error.message for error in errors]}"
                checked.append(path)
    assert len(checked) == 28


def test_peer_enrollment_admission_contract():
    crd = load_crds(ROOT / "crds")["netbird-peer-enrollment.yaml"]
    spec = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]
    assert spec["properties"]["mode"]["enum"] == ["enroll", "adopt"]
    assert spec["properties"]["deletionPolicy"]["default"] == "Retain"
    assert spec["properties"]["enrollment"]["properties"]["expiresInSeconds"]["maximum"] == 3600
    rules = [item["rule"] for item in spec["x-kubernetes-validations"]]
    assert any("has(self.peerIdentity.id)" in rule for rule in rules)
    assert any("ownership.takeOwnership" in rule for rule in rules)
