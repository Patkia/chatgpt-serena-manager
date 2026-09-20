# Security

## Keep local secrets local

Never commit or publish:

- control-plane API keys or tokens
- DPAPI credential files (`*.dpapi`)
- tunnel IDs, generated tunnel profiles, or account-specific endpoints
- private keys, certificates, `.env` files, logs, backups, crash traces, or local project paths

The supplied `.gitignore` covers common runtime artifacts. It is a guardrail, not a guarantee. Review these before every commit:

```powershell
git status --short
git diff --cached
```

## Local code access

Serena project configuration can permit file reads, writes, and shell commands. Add only projects you trust, restrict filesystem permissions where appropriate, review every code change, and stop tunnels that are not in use.

## Reporting a vulnerability

Do not open a public issue containing credentials, tunnel IDs, private project paths, or reproduction logs. Use the repository maintainer’s private contact channel once one is published. Until then, share only a sanitized description and request a private reporting method.
