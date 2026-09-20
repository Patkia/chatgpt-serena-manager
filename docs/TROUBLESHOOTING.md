# Troubleshooting

## Serena RUNNING, Tunnel STOPPED (PARTIAL)

Open the project log from the Manager and inspect `launcher.log`, `tunnel.log`, and `tunnel.stderr.log` locally. Confirm the tunnel profile exists in `%APPDATA%\tunnel-client`, the tunnel client is installed, and the health port is available. Never paste credentials or a full tunnel ID into a public issue.

## Node.js or language server error

Some Serena language servers require Node.js. Check:

```powershell
node -v
npm -v
where.exe node
```

After installing Node.js, close and reopen Serena Manager before retrying.

## Port conflict

A port can be reserved by another configured launcher even if it is not currently listening. Check the Manager table and any local launcher configuration before changing ports. Do not reuse a port pair assigned to another project.

## Plugin creation fails or ChatGPT cannot connect

Start the project first and wait until Serena and Tunnel are both **RUNNING**. Verify the active local project, the tunnel profile, and the custom MCP/plugin configuration. Create the plugin using your own tunnel ID; do not use an example value.

## Tunnel profile error

Check the local profile syntax and the exact local credential-file path expected by the launcher. Keep the profile and credential outside Git. Re-create an account-issued tunnel/profile if it was revoked; do not copy another project’s credential.

## Manager does not open

Run:

```powershell
py -3 .\Serena-Manager.py
```

Confirm Python includes Tkinter and that `pyw.exe -3 --version` succeeds. The hidden launcher deliberately suppresses console output.
