# Installation (Windows)

## 1. Install prerequisites

Install Python 3 with Tkinter and the Python launcher. Confirm:

```powershell
py -3 --version
pyw.exe -3 --version
```

Install Serena following its own documentation, then confirm it is on `PATH`:

```powershell
serena --help
```

Install the Secure MCP Tunnel client supplied for your account. Keep its executable and credential outside the repository.

## 2. Clone or copy this repository

Place the Manager in a tools folder. By default it discovers sibling launcher folders, for example:

```text
%USERPROFILE%\Tools\Serena-manager
%USERPROFILE%\Tools\Serena-example-project
```

If you use another location, set `SERENA_TOOLS_ROOT` to the folder that contains all `Serena-*` launcher directories before starting the Manager.

## 3. Configure local-only credentials

Create your control-plane API key file locally, encrypted for your Windows account if your tunnel client uses DPAPI. The current launcher convention expects:

```text
%USERPROFILE%\Tools\control-plane-api-key.dpapi
```

Do not place this file in the repository. Do not paste the key into a launcher, README, issue, or commit.

## 4. Install the tunnel client locally

The Add Project flow needs a local tunnel executable. Its default convention is:

```text
%USERPROFILE%\Tools\tunnel-client-v0.0.14-windows-amd64\tunnel-client.exe
```

If your installation differs, use the existing launcher configuration pattern or adapt your local setup outside version control. Do not commit account-specific profiles from `%APPDATA%\tunnel-client`.

## 5. Start the Manager

Double-click `Start-Serena-Manager.cmd`. It invokes the hidden VBS launcher, so no Command Prompt should remain open.

If it does not open, run the script from PowerShell to see errors:

```powershell
py -3 .\Serena-Manager.py
```

## 6. Add the first project

Use **+** in the Manager, select an existing source folder, choose a safe project name, and enter a tunnel ID issued for that project. The Manager writes local Serena metadata, a launcher folder, and a tunnel profile, then validates the generated setup.

The generated project config enables writing. Use a disposable test repository first and review the generated files.
