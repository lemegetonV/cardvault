# Error catalog

Every error response has this shape:

```json
{"error_code": "INVALID_CREDENTIALS", "message": "Username or password is incorrect",
 "correlation_id": "6f1c...", "details": {}}
```

`details` is present only when it adds something (validation errors, lockout end time).
The list grows with each phase.

## Common (every service)

| error_code | HTTP | When |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Request body, query or path failed validation. `details` lists `loc`, `msg`, `type` |
| `UNAUTHORIZED` | 401 | A customer endpoint was called without a bearer token |
| `TOKEN_INVALID` | 401 | Bearer token malformed, wrong signature, issuer, audience or unknown `kid` |
| `TOKEN_EXPIRED` | 401 | Access token `exp` is in the past on the shared clock |
| `SERVICE_KEY_INVALID` | 401 | Internal endpoint called without a valid `X-Service-Key` |
| `TEST_KEY_INVALID` | 401 | `/test/*` called without a valid `X-Test-Key` |
| `NOT_FOUND` | 404 | No such route or resource |
| `METHOD_NOT_ALLOWED` | 405 | Wrong HTTP method for the route |
| `INTERNAL_ERROR` | 500 | Unexpected server error |
| `FAULT_INJECTED` | 5xx | An injected `error` fault fired (status as configured, default 503) |
| `FAULT_TIMEOUT` | 504 | An injected `timeout` fault fired |

## Callback signatures (used from phase 3)

| error_code | HTTP | When |
|---|---|---|
| `SIGNATURE_MISSING` | 401 | `X-Signature` or `X-Timestamp` missing |
| `SIGNATURE_INVALID` | 401 | Signature does not match, or `X-Timestamp` is not Unix seconds |
| `SIGNATURE_EXPIRED` | 401 | `X-Timestamp` more than 300 s from the shared clock |

## auth-service

| error_code | HTTP | When |
|---|---|---|
| `INVALID_CREDENTIALS` | 401 | Unknown username or wrong password (deliberately the same) |
| `ACCOUNT_LOCKED` | 423 | 5 failed logins for this username in a row; locked 15 minutes. `details.locked_until` |
| `REFRESH_TOKEN_INVALID` | 401 | Unknown or revoked refresh token |
| `REFRESH_TOKEN_REUSED` | 401 | An already-used refresh token was presented; the whole family is now revoked |
| `REFRESH_TOKEN_EXPIRED` | 401 | Refresh token older than 7 days on the shared clock |
| `USER_EXISTS` | 409 | `POST /test/users` with a username or customer_id that exists |
| `SEED_CONFIG_MISSING` | 500 | `POST /test/seed` without `SEED_USER_PASSWORD` configured |
