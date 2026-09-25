# NetBird declarative limitations

## Status and scope

The remote-access implementation that depends on JWT-backed groups is deferred.
This document records the NetBird behavior that blocks a deterministic
declaration; it intentionally does not prescribe an implementation, workaround,
or fork.

The observations apply to the self-hosted NetBird account plane and API used by
this project. They should be revalidated against the deployed NetBird version
before the deferred work resumes.

## JWT groups do not exist before identity synchronization

NetBird can map identity-provider claims to account groups through JWT group
synchronization. Enabling and allowing a JWT group name configures the mapping,
but does not create the corresponding NetBird group object.

The group is materialized when a user with the matching claim logs in and the
identity synchronization path processes that login. Until then there is no
stable group ID for policies, DNS configuration, routes, network resources, or
manual peer membership to reference.

This makes the desired state dependent on an interactive event outside the
reconciliation loop. In particular, an administrator cannot assign a peer to a
JWT-backed group before at least one matching user has logged in.

References:

- [NetBird identity-provider group synchronization](https://docs.netbird.io/selfhosted/identity-providers)
- [NetBird Groups API](https://docs.netbird.io/api/resources/groups)

## API-created and JWT-created groups are not equivalent

NetBird records the issuer of a group. Groups created through the public API are
API-issued, while groups materialized by identity synchronization are
JWT-issued. Creating an API group with the desired name does not make it the
target of later JWT synchronization, and the public Groups API does not expose a
way to create a JWT-issued group.

Consequently, group name alone is not a declarative identity for this case. A
same-name API group cannot safely stand in for a not-yet-created JWT group.

References:

- [NetBird documentation: same-name NetBird and IdP groups do not sync](https://docs.netbird.io/selfhosted/identity-providers#jwt-group-sync)
- [NetBird issue #2685: API-created groups cannot be assigned through JWT sync](https://github.com/netbirdio/netbird/issues/2685)

## First-login creation is not a deterministic uniqueness boundary

Group materialization happens in the login request path rather than through a
single account-plane reconciliation boundary. An upstream report and source
analysis documents a race in which concurrent synchronization can create
duplicate same-name groups. The analyzed account data does not provide a
uniqueness guarantee on the group name that consumers could rely on as a stable
key.

Once duplicates exist, resolving a name to one group ID becomes ambiguous for
any declarative controller.

References:

- [NetBird discussion #7241: JWT group synchronization behavior](https://github.com/netbirdio/netbird/discussions/7241)

## References are IDs but desired configuration is written with names

Policies and the other account-plane resources ultimately reference NetBird
group IDs. Our Kubernetes resources use names so configuration can be reviewed
and moved between accounts. Name resolution is deterministic only when exactly
one matching object already exists.

For a JWT-backed group, the group can be absent before login or ambiguous after
duplicate creation. A controller can therefore report an unresolved reference,
but it cannot derive the intended stable ID from the desired configuration.

## Account settings updates are whole-object writes

The account endpoint uses a `PUT` request for settings. A sparse desired change
is not a patch: fields omitted from the request can be overwritten by server
defaults. Multiple independent reconcilers writing subsets of account settings
can therefore reset one another unless every update first preserves the current
values.

Reference: [NetBird Accounts API](https://docs.netbird.io/api/resources/accounts)

## Operational consequence

The following state cannot currently be declared end to end before a user logs
in:

1. a JWT-backed group exists exactly once;
2. peers can be assigned manually to that group;
3. all policies, DNS settings, routes, and network resources resolve the same
   stable group ID; and
4. reconciliation produces the same result independently of login timing.

Configurations that assume those properties remain incomplete and must not be
treated as proof that remote access is fully provisioned.
