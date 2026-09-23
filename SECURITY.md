# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.x     | :white_check_mark: |

## Security Architecture & Design

`agy-multi` is designed with security and privacy as primary design goals:

1. **No Built-in OAuth Client Secret**:
   - The repository does **not** ship an OAuth client secret (or client id) in source.
   - Token refresh for quota APIs requires `AGY_OAUTH_CLIENT_ID` / `AGY_OAUTH_CLIENT_SECRET`
     (aliases: `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET`), or a fresh login via `agy` / `agy-multi login`.
   - Missing credentials cause refresh to fail with a clear error; they do not crash the process.
2. **Strict File Permissions**:
   - Profile sandbox directories (`~/.gemini-profiles/<name>/`) are created with `0700` permissions.
   - OAuth tokens (`antigravity-oauth-token`) and account registry files (`accounts.json`) are enforced with `0600` permissions after write/import.
3. **Sensitive Directory Exclusion**:
   - Host environment symlinking strictly excludes sensitive paths: `.ssh`, `.gnupg`, `.gpg`, `.aws`, `.azure`, `.kube`, `.docker`, `.netrc`, `.vault-token`, and `.git-credentials`.
4. **Network Exposure & LAN Auth**:
   - The web dashboard (`agy-multi serve`) binds to `127.0.0.1` by default.
   - Cross-Origin Resource Sharing (CORS) is restricted to loopback/local origins.
   - **Loopback + no token**: local convenience — GET `/api/*` and the HTML dashboard may be accessed without a token from the local machine; mutating `POST` endpoints still require a token when one is configured.
   - **Non-loopback bind and/or `AGY_MULTI_SERVER_TOKEN` / `--token` configured**: **all** endpoints — including GET `/api/*` and the HTML dashboard — require a valid `Authorization: Bearer …` or `X-API-Token` header. Binding to a LAN/public interface without a token auto-generates one at startup.

## Credential rotation after history exposure

If this repository (or any fork/mirror/CI cache) ever contained Google OAuth client secrets in git history, treat those credentials as **compromised**:

1. In Google Cloud Console, **rotate/delete** the exposed OAuth client secret as soon as you have access to a browser.
2. Request GitHub removal of unreachable sensitive commits if old SHAs remain fetchable after a history rewrite ([docs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)).
3. Keep the repo **private** until rotation is done and old SHAs return 404.

Current `main` does **not** ship built-in client secrets; refresh requires `AGY_OAUTH_CLIENT_ID` / `AGY_OAUTH_CLIENT_SECRET` (or `GOOGLE_OAUTH_*` aliases).

## Reporting a Vulnerability

If you discover a security vulnerability within `agy-multi`, please do **not** open a public issue.

Instead, please send an advisory or report directly to the repository maintainers via GitHub Private Vulnerability Reporting or by contacting the project maintainers directly.

Please include:
- A description of the vulnerability and its potential impact.
- Steps to reproduce or proof-of-concept code.
- Affected versions and environment details.

We will review the submission, investigate the issue, and provide a patch in a timely manner.
