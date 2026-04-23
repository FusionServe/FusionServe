---
title: Security
---
Since FusionServe is database-first security checks and authorization must be
implemented in the data schema itself. This grants that the database itself is
the single source of thruth even in the critical security level.

Combining PostgreSQL permissions system (based on roles) and
[Row-Level Security](https://www.postgresql.org/docs/current/static/ddl-rowsecurity.html)
(RLS) policies
we can push security checks in the lowest level possible, the data itself.
In this way every service that
interacts wih the data is protected by the same logic, maintained in a single
place. You can make every additional service talking directly to
the database and be assured that your data is properly proteceted.

When Row Level Security (RLS) is enabled, all rows are by default not visible to
any roles (except database administration roles and the role who created the
database/table); permission is selectively granted with the use of policies.

## Authentication strategy

Since a typical web application nowadays is a SPA &/or PWA frontent talking with
the backend via api the authentication and authorization system implemented by
FusionServe is Authorization header with the Bearer authentication scheme and JWTs.
Authentication is demanded to the identity provider and the default out of the box
configuration works and is tested with Keycloak. It verifies the JWTs retrieving
from the keys from the provider JWKS endpoint. You only need to specify the
JWT_ISSUER or JWKS_URL and CLIENT_ID parameters. The issuer is only needed to
verify that the token is actually signed by a trusted provider and the CLIENT_ID
is needed to extract roles from the jwt since inside a token there are many role
sets and we need to identify which one to consider, e.g:

```json
{
  "exp": 1776933148,
  "iat": 1776932848,
  "sub": "<user id>",
  "resource_access": {
    "realm-management": {
      "roles": [
        "impersonation",
        "manage-users",
        "view-users",
        "query-users"
      ]
    },
    "<client id>": {
      "roles": [
        "role1",
        "user"
      ]
    }
  },
  "scope": "openid email profile",
  "name": "John Doe",
  "preferred_username": "jdoe",
  "given_name": "John",
  "family_name": "Doe",
  "email": "jdoe@example.com"
}
```
Using the configured [JSON Pointers](https://datatracker.ietf.org/doc/html/rfc6901)
the system create a `User` istance in the Litestar Request which is used by the
`set_role()` function to generate the sql to set role and config:

```sql
select set_config('role', 'role1', true), set_config('user.id', '<uuid>', true), ...
```
making it available in the database context.

## Using values inside PostgreSQL

Inside PostgreSQL you can read these values with `current_setting`, at startup
a function is created to ease the use inside rls:

```sql
CREATE OR REPLACE FUNCTION app_public.current_user_id()
 RETURNS uuid
 LANGUAGE sql
 STABLE
AS $function$
  SELECT current_setting('user.id', true)::uuid;
$function$
;
```

Apply the function (or the `current_setting` call directly) inside Row Level
Security policies to enforce the rules, so the PostgreSQL documentation [example](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)

```sql
CREATE POLICY user_policy ON users
    USING (user_name = current_user);
```
becomes:
```sql
CREATE POLICY user_policy ON users
    USING (user_id = current_user_id());
```
or, using `current_settings` retrieving all the other User fields from the "user."
namespace in settings:
```sql
CREATE POLICY user_policy ON users
    USING (user_name = current_setting('user.username', true));
```

## Roles strategies

The configuration option ANONYMOUS_ROLE is the role assigned to unauthenticated
users. Set it to the same value as PG_USER actually disables roles enforcing making
**all** the database tables publicy accessible.
There are two strategies regarding user roles:
1. a user has only one role (PostGraphile style)
2. users may have many roles (Hasura style)
In the database level only one role may be active during a transaction so in the
second case we must determine the relevant role to set at query time. By default
the first role in the JWT roles list is set in the Request User object.
**TODO**: handle more roles.
