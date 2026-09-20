# Usage

The Serena Manager table displays one row for each discovered launcher folder.

| Column | Meaning |
| --- | --- |
| Project | Launcher/project label |
| Project Path | Local folder passed to Serena |
| Serena | Local Serena MCP service state |
| Tunnel | Secure tunnel service state |
| MCP Port | Local MCP listener port |
| Health Port | Tunnel health listener port |
| Status | Combined service state |

## Status values

- **RUNNING**: Serena and tunnel are both running and the tunnel health endpoint is ready.
- **STOPPED**: both services are stopped and their expected ports are free.
- **PARTIAL**: only one service is running.
- **ERROR**: a launcher cannot be parsed, a listener belongs to an unexpected process, or readiness fails.

## Buttons

| Button | Action |
| --- | --- |
| Refresh | Re-check all project states without blocking the UI. |
| Start | Run the selected project’s normal hidden launcher. |
| Stop | Run the selected project’s scoped Stop launcher. |
| Restart | Stop, then start the selected project. |
| Debug | Open the selected project’s debug launcher with console output. |
| Log | Open the selected project’s launcher log. |
| Stop all | Stop every project currently reported as running. |
| + | Add a new Manager-owned project. |
| X | Safely remove a Manager-owned project only; source files are not removed. |

Drag a row with the left mouse button to reorder projects. The order is stored in the current user’s application-data folder and is not shared through Git.

Before connecting ChatGPT, refresh and confirm **Serena = RUNNING**, **Tunnel = RUNNING**, and **Status = RUNNING**.
