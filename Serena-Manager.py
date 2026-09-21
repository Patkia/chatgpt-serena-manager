from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from serena_project_creator import (
    OwnershipError,
    create_project_setup,
    preview_project,
    remove_managed_project,
    sanitize_project_name,
    validate_tunnel_id,
    verify_managed_project,
)

MANAGER_ROOT = Path(__file__).resolve().parent
# By default, launchers live beside the manager directory (for example
# %USERPROFILE%\Tools\Serena-manager and %USERPROFILE%\Tools\Serena-my-project).
# Set SERENA_TOOLS_ROOT when using a different layout.
TOOLS_ROOT = Path(os.environ.get("SERENA_TOOLS_ROOT", str(MANAGER_ROOT.parent))).expanduser()
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
ORDER_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "Serena-manager" / "project-order.json"
STALE_ENTRY_FILE = ORDER_FILE.with_name("stale-project-entries.json")
ADD_ERROR_LOG = MANAGER_ROOT / "logs" / "add-project-errors.log"

@dataclass
class Project:
    name: str
    folder: Path
    project_path: str = ""
    mcp_port: int = 0
    health_port: int = 0
    profile: str = ""
    tunnel_client: str = ""
    start_cmd: Path | None = None
    stop_cmd: Path | None = None
    restart_cmd: Path | None = None
    debug_cmd: Path | None = None
    log_file: Path | None = None
    parse_error: str = ""

@dataclass
class ServiceState:
    state: str
    detail: str

@dataclass
class ProjectState:
    serena: ServiceState
    tunnel: ServiceState
    status: str

STRING_ASSIGNMENT = re.compile(r"^\s*\$(project|tunnelClient|profile)\s*=\s*'([^']*)'\s*$", re.MULTILINE)
PORT_ASSIGNMENT = re.compile(r"^\s*\$(serenaPort|tunnelPort)\s*=\s*(\d+)\s*$", re.MULTILINE)

def discover_projects(tools_root: Path = TOOLS_ROOT) -> list[Project]:
    projects: list[Project] = []
    hidden_stale_entries = load_stale_entries()
    for folder in sorted(tools_root.glob("Serena-*"), key=lambda p: p.name.casefold()):
        if not folder.is_dir():
            continue
        launchers = sorted(folder.glob("Start-Serena-*.ps1"))
        if not launchers:
            continue
        if len(launchers) != 1:
            projects.append(Project(folder.name.removeprefix("Serena-"), folder, parse_error="Expected exactly one Start PS1"))
            continue
        launcher = launchers[0]
        match = re.fullmatch(r"Start-Serena-(.+)\.ps1", launcher.name, re.IGNORECASE)
        name = match.group(1) if match else folder.name.removeprefix("Serena-")
        project = Project(name=name, folder=folder)
        try:
            text = launcher.read_text(encoding="utf-8-sig")
            values = dict(STRING_ASSIGNMENT.findall(text))
            values.update(dict(PORT_ASSIGNMENT.findall(text)))
            required = ("project", "tunnelClient", "profile", "serenaPort", "tunnelPort")
            missing = [key for key in required if key not in values]
            if missing:
                raise ValueError("Cannot parse: " + ", ".join(missing))
            project.project_path = values["project"]
            project.tunnel_client = values["tunnelClient"]
            project.profile = values["profile"]
            project.mcp_port = int(values["serenaPort"])
            project.health_port = int(values["tunnelPort"])
            project.start_cmd = launcher.with_suffix(".cmd")
            project.stop_cmd = folder / f"Stop-Serena-{name}.cmd"
            project.restart_cmd = folder / f"Restart-Serena-{name}.cmd"
            project.debug_cmd = folder / f"Start-Serena-{name}-Debug.cmd"
            project.log_file = folder / "logs" / "launcher.log"
            if not project.start_cmd.is_file():
                raise ValueError(f"Missing {project.start_cmd.name}")
            if not project.stop_cmd.is_file():
                raise ValueError(f"Missing {project.stop_cmd.name}")
        except (OSError, ValueError) as exc:
            project.parse_error = str(exc)
        if not is_hidden_stale_entry(project, hidden_stale_entries):
            projects.append(project)
    return projects

def load_project_order() -> list[str]:
    try:
        value = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
        return [str(name) for name in value if isinstance(name, str)] if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []

def order_projects(projects: list[Project]) -> list[Project]:
    saved = load_project_order()
    by_name = {project.name: project for project in projects}
    ordered = [by_name[name] for name in saved if name in by_name]
    ordered.extend(project for project in projects if project.name not in saved)
    return ordered

def save_project_order(projects: list[Project]) -> None:
    try:
        ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
        ORDER_FILE.write_text(json.dumps([project.name for project in projects], ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

def load_stale_entries() -> list[dict[str, str]]:
    try:
        payload = json.loads(STALE_ENTRY_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, TypeError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    result: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        project_path = entry.get("project_path")
        launcher_folder = entry.get("launcher_folder")
        if all(isinstance(value, str) and value for value in (name, project_path, launcher_folder)):
            result.append({"name": name, "project_path": project_path, "launcher_folder": launcher_folder})
    return result

def is_hidden_stale_entry(project: Project, entries: list[dict[str, str]] | None = None) -> bool:
    if not project.project_path or Path(project.project_path).is_dir():
        return False
    for entry in entries if entries is not None else load_stale_entries():
        if (entry["name"] == project.name
                and os.path.normcase(entry["project_path"]) == os.path.normcase(project.project_path)
                and os.path.normcase(entry["launcher_folder"]) == os.path.normcase(str(project.folder))):
            return True
    return False

def query_project_processes(project: Project) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    script = (
        "$rows=@(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {$_.CommandLine} | ForEach-Object {"
        "[PSCustomObject]@{pid=[int]$_.ProcessId;name=[string]$_.Name;command_line=[string]$_.CommandLine}});"
        "if($rows.Count -eq 0){'[]'}else{ConvertTo-Json -Compress -Depth 3 -InputObject $rows}"
    )
    rows = _powershell_json(script)
    return (
        [row for row in rows if matches_serena(str(row.get("command_line", "")), project)],
        [row for row in rows if matches_tunnel(str(row.get("command_line", "")), project)],
    )

def verify_stale_entry_removal(project: Project) -> None:
    if project.parse_error or not project.project_path:
        raise OwnershipError("REMOVE BLOCKED — stale entry identity could not be parsed")
    if Path(project.project_path).is_dir():
        raise OwnershipError("REMOVE BLOCKED — project source folder still exists")
    listeners = query_ports([project.mcp_port, project.health_port])
    if listeners.get(project.mcp_port) or listeners.get(project.health_port):
        raise OwnershipError("REMOVE BLOCKED — project MCP or health port is still listening")
    serena_processes, tunnel_processes = query_project_processes(project)
    if serena_processes or tunnel_processes:
        raise OwnershipError("REMOVE BLOCKED — matching Serena or tunnel process is still running")
    state = get_project_state(project, listeners)
    if state.status != "STOPPED":
        raise OwnershipError(f"REMOVE BLOCKED — project is not stopped ({state.status})")

def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".serena-manager.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)

def remove_stale_entry(project: Project) -> None:
    verify_stale_entry_removal(project)
    try:
        order_payload = json.loads(ORDER_FILE.read_text(encoding="utf-8")) if ORDER_FILE.is_file() else []
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"REMOVE BLOCKED — Manager project order is invalid: {exc}") from exc
    if not isinstance(order_payload, list) or any(not isinstance(name, str) for name in order_payload):
        raise RuntimeError("REMOVE BLOCKED — Manager project order is invalid")
    original_order = ORDER_FILE.read_bytes() if ORDER_FILE.is_file() else None
    original_stale = STALE_ENTRY_FILE.read_bytes() if STALE_ENTRY_FILE.is_file() else None
    entry = {"name": project.name, "project_path": project.project_path, "launcher_folder": str(project.folder)}
    entries = [item for item in load_stale_entries() if item != entry]
    entries.append(entry)
    try:
        _write_json_atomic(ORDER_FILE, [name for name in order_payload if name != project.name])
        _write_json_atomic(STALE_ENTRY_FILE, {"version": 1, "entries": entries})
    except Exception as exc:
        try:
            if original_order is None:
                ORDER_FILE.unlink(missing_ok=True)
            else:
                ORDER_FILE.write_bytes(original_order)
            if original_stale is None:
                STALE_ENTRY_FILE.unlink(missing_ok=True)
            else:
                STALE_ENTRY_FILE.write_bytes(original_stale)
        except OSError:
            pass
        raise RuntimeError(f"REMOVE BLOCKED — Manager stale entry state could not be saved: {exc}") from exc

def _powershell_json(script: str) -> list[dict[str, object]]:
    completed = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=12,
        creationflags=CREATE_NO_WINDOW, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"PowerShell exited {completed.returncode}")
    value = json.loads(completed.stdout.strip() or "[]")
    return [value] if isinstance(value, dict) else value

def query_ports(ports: list[int]) -> dict[int, list[dict[str, object]]]:
    if not ports or any(not 1 <= int(port) <= 65535 for port in ports):
        raise ValueError("Invalid TCP port")
    requested = ",".join(str(int(port)) for port in ports)
    script = (
        f"$wanted=@({requested});$all=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {{$wanted -contains [int]$_.LocalPort}});"
        "$rows=@($all | ForEach-Object {"
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId = $($_.OwningProcess)\" -ErrorAction SilentlyContinue;"
        "if($p){[PSCustomObject]@{port=[int]$_.LocalPort;pid=[int]$p.ProcessId;name=[string]$p.Name;command_line=[string]$p.CommandLine}}});"
        "if($rows.Count -eq 0){'[]'}else{ConvertTo-Json -Compress -Depth 3 -InputObject $rows}"
    )
    rows = _powershell_json(script)
    result = {int(port): [] for port in ports}
    for row in rows:
        result.setdefault(int(row.get("port", 0)), []).append(row)
    return result

def query_port(port: int) -> list[dict[str, object]]:
    return query_ports([port]).get(int(port), [])

def matches_serena(command_line: str, project: Project) -> bool:
    line = command_line or ""
    return (re.search(r"serena", line, re.I) is not None and re.search(r"\bstart-mcp-server\b", line, re.I) is not None
            and project.project_path.casefold() in line.casefold()
            and re.search(rf"--port\s+{project.mcp_port}(?:\s|$)", line, re.I) is not None)

def matches_tunnel(command_line: str, project: Project) -> bool:
    line = command_line or ""
    profile = re.escape(project.profile)
    return (re.search(r"tunnel-client(?:\.exe)?", line, re.I) is not None and re.search(r"\brun\b", line, re.I) is not None
            and re.search(rf"(?:--profile\s+[\"']?{profile}[\"']?|profile[=:]\s*{profile})(?:\s|$)", line, re.I) is not None)

def classify_service(listeners: list[dict[str, object]], matcher: Callable[[str], bool]) -> ServiceState:
    if not listeners:
        return ServiceState("STOPPED", "Port is free")
    unexpected = [row for row in listeners if not matcher(str(row.get("command_line", "")))]
    if unexpected:
        pids = ", ".join(str(row.get("pid", "?")) for row in unexpected)
        return ServiceState("ERROR", f"PORT CONFLICT (PID {pids})")
    return ServiceState("RUNNING", "Expected process owns port")

def tunnel_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/readyz", timeout=2) as response:
            return response.status == 200 and response.read().decode("utf-8", "replace").strip().lower() == "ready"
    except (OSError, urllib.error.URLError, TimeoutError):
        return False

def get_project_state(project: Project, all_listeners: dict[int, list[dict[str, object]]] | None = None) -> ProjectState:
    if project.parse_error:
        error = ServiceState("ERROR", project.parse_error)
        return ProjectState(error, error, "ERROR")
    try:
        listeners = all_listeners if all_listeners is not None else query_ports([project.mcp_port, project.health_port])
        serena = classify_service(listeners[project.mcp_port], lambda line: matches_serena(line, project))
        tunnel = classify_service(listeners[project.health_port], lambda line: matches_tunnel(line, project))
        if tunnel.state == "RUNNING":
            tunnel = ServiceState("RUNNING", "Expected process owns port; /readyz=ready") if tunnel_ready(project.health_port) else ServiceState("ERROR", "Expected tunnel owns port but /readyz is not ready")
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        error = ServiceState("ERROR", str(exc))
        return ProjectState(error, error, "ERROR")
    if "ERROR" in (serena.state, tunnel.state): status = "ERROR"
    elif serena.state == tunnel.state == "RUNNING": status = "RUNNING"
    elif serena.state == tunnel.state == "STOPPED": status = "STOPPED"
    else: status = "PARTIAL"
    return ProjectState(serena, tunnel, status)

def get_project_states(projects: list[Project]) -> dict[str, ProjectState]:
    ports = sorted({port for project in projects if not project.parse_error for port in (project.mcp_port, project.health_port)})
    listeners = query_ports(ports) if ports else {}
    return {project.name: get_project_state(project, listeners) for project in projects}

def run_launcher(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=75,
                          creationflags=CREATE_NO_WINDOW, check=False)

def start_launcher(path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )

def logic_test() -> None:
    sample = Project("sample", Path("."), r"C:\work\sample", 8123, 8183, "serena-sample")
    good_serena = r'python.exe serena start-mcp-server --project "C:\work\sample" --port 8123'
    bad_serena = r'python.exe serena start-mcp-server --project "C:\work\other" --port 8123'
    good_tunnel = r'tunnel-client.exe run --profile serena-sample'
    bad_tunnel = r'tunnel-client.exe run --profile serena-other'
    assert matches_serena(good_serena, sample) and not matches_serena(bad_serena, sample)
    assert matches_tunnel(good_tunnel, sample) and not matches_tunnel(bad_tunnel, sample)
    conflict = classify_service([{"pid": 42, "command_line": bad_serena}], lambda line: matches_serena(line, sample))
    assert conflict.state == "ERROR" and "PORT CONFLICT" in conflict.detail

class ManagerApp:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk
        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.root = tk.Tk()
        self.root.title("Serena Manager")
        self.root.geometry("1180x520")
        self.root.configure(background="#f7f8fa")
        style = ttk.Style(self.root)
        style.configure("Main.TFrame", background="#f7f8fa")
        style.configure("Actions.TFrame", background="#f7f8fa")
        style.configure("Manager.Treeview", font=("Segoe UI", 10), rowheight=30,
                        background="#ffffff", fieldbackground="#ffffff", foreground="#243447",
                        bordercolor="#cbd5e1", lightcolor="#cbd5e1", darkcolor="#cbd5e1")
        style.configure("Manager.Treeview.Heading", font=("Segoe UI", 10, "bold"),
                        foreground="#334155", background="#eef3f8", relief="flat", padding=(9, 8))
        style.map("Manager.Treeview", background=[("selected", "#dbeafe")],
                  foreground=[("selected", "#1e3a5f")])
        style.map("Manager.Treeview.Heading", background=[("active", "#e6edf5")])
        style.configure("Action.TButton", font=("Segoe UI", 10), padding=(10, 5))
        style.configure("Status.TLabel", font=("Segoe UI", 10), foreground="#475569", background="#f7f8fa")
        self.projects: list[Project] = []
        self.states: dict[str, ProjectState] = {}
        self.busy = False
        self.drag_name: str | None = None
        frame = ttk.Frame(self.root, style="Main.TFrame", padding=(14, 14, 14, 10)); frame.pack(fill="both", expand=True)
        columns = ("project", "path", "serena", "tunnel", "mcp", "health", "status")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", style="Manager.Treeview")
        headings = {"project":"Project","path":"Project Path","serena":"Serena","tunnel":"Tunnel","mcp":"MCP Port","health":"Health Port","status":"Status"}
        widths = {"project":180,"path":350,"serena":90,"tunnel":90,"mcp":80,"health":90,"status":110}
        for column in columns:
            anchor = "w" if column in ("project", "path") else "center"
            self.tree.heading(column, text=headings[column], anchor=anchor)
            self.tree.column(column, width=widths[column], anchor=anchor)
        for tag, color in (("RUNNING","#e1f5e7"),("STOPPED","#f7f8fa"),("PARTIAL","#fff7dc"),("ERROR","#fde8e8")):
            self.tree.tag_configure(tag, background=color, foreground="#243447")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview); scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set); self.tree.bind("<Double-1>", self.inspect_selected)
        self.tree.bind("<ButtonPress-1>", self._drag_start)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_end)
        self.root.bind("<F5>", lambda _event: self.refresh())
        buttons = ttk.Frame(self.root, style="Actions.TFrame", padding=(14, 2, 14, 12)); buttons.pack(fill="x")
        def action_button(label: str, hint: str, command: Callable[[], None]) -> None:
            button = ttk.Button(buttons, text=label, style="Action.TButton", command=command)
            button.pack(side="left", padx=(0, 6))
            tip: dict[str, object] = {"window": None}
            def show_tip(_event: object) -> None:
                if tip["window"] is not None: return
                window = tk.Toplevel(self.root); window.overrideredirect(True); window.attributes("-topmost", True)
                window.geometry(f"+{button.winfo_rootx()}+{button.winfo_rooty() + button.winfo_height() + 4}")
                tk.Label(window, text=hint, padx=7, pady=3, relief="solid", borderwidth=1, background="#fffbe6").pack()
                tip["window"] = window
            def hide_tip(_event: object) -> None:
                window = tip["window"]
                if window is not None: window.destroy(); tip["window"] = None
            button.bind("<Enter>", show_tip); button.bind("<Leave>", hide_tip)
        action_button("▶ Start", "Start project ที่เลือก", self.start_selected)
        action_button("■ Stop", "Stop project ที่เลือก", self.stop_selected)
        action_button("↻ Restart", "Restart project ที่เลือก", self.restart_selected)
        action_button("▣ Log", "เปิด log", self.open_log)
        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=(2, 8), pady=4)
        action_button("＋ Add", "เพิ่ม Serena project", self.open_add_dialog)
        action_button("✕ Remove", "Remove Serena integration ที่เลือก", self.open_remove_dialog)
        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=(2, 8), pady=4)
        action_button("⏹ Stop All", "Stop ทุก project ที่กำลังทำงาน", self.stop_all)
        self.status_var = tk.StringVar(value="Ready"); ttk.Label(buttons, textvariable=self.status_var, style="Status.TLabel").pack(side="right", padx=4)
        self.refresh()

    def selected_project(self) -> Project | None:
        selected = self.tree.selection()
        if not selected:
            self.messagebox.showinfo("Serena Manager", "Select a project first."); return None
        return next((project for project in self.projects if project.name == selected[0]), None)

    def _drag_start(self, event: object) -> None:
        if self.busy: return
        row = self.tree.identify_row(event.y)
        self.drag_name = row or None

    def _drag_motion(self, event: object) -> None:
        if self.busy or not self.drag_name: return
        target = self.tree.identify_row(event.y)
        if not target or target == self.drag_name: return
        current_index = next((index for index, project in enumerate(self.projects) if project.name == self.drag_name), -1)
        target_index = self.tree.index(target)
        if current_index < 0 or current_index == target_index: return
        project = self.projects.pop(current_index)
        self.projects.insert(target_index, project)
        self.tree.move(self.drag_name, "", target_index)
        self.tree.selection_set(self.drag_name)

    def _drag_end(self, _event: object) -> None:
        if self.drag_name:
            save_project_order(self.projects)
        self.drag_name = None

    def open_add_dialog(self) -> None:
        if self.busy: return
        from tkinter import filedialog

        dialog = self.tk.Toplevel(self.root)
        dialog.title("Add Serena Project")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        folder_var = self.tk.StringVar()
        tunnel_var = self.tk.StringVar()
        name_var = self.tk.StringVar()
        summary_var = self.tk.StringVar(value="เลือก Project Folder เพื่อดูรายละเอียด")
        progress_var = self.tk.StringVar(value="Ready")
        auto_name = {"value": ""}

        body = self.ttk.Frame(dialog, padding=14); body.pack(fill="both", expand=True)
        self.ttk.Label(body, text="Project Folder:").grid(row=0, column=0, sticky="w", pady=4)
        folder_entry = self.ttk.Entry(body, textvariable=folder_var, width=58)
        folder_entry.grid(row=0, column=1, sticky="ew", pady=4)

        self.ttk.Label(body, text="Tunnel ID:").grid(row=1, column=0, sticky="w", pady=4)
        tunnel_entry = self.ttk.Entry(body, textvariable=tunnel_var, width=58)
        tunnel_entry.grid(row=1, column=1, sticky="ew", pady=4)

        self.ttk.Label(body, text="Project Name:").grid(row=2, column=0, sticky="w", pady=4)
        name_entry = self.ttk.Entry(body, textvariable=name_var, width=58)
        name_entry.grid(row=2, column=1, sticky="ew", pady=4)

        summary = self.ttk.LabelFrame(body, text="Summary", padding=10)
        summary.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.ttk.Label(summary, textvariable=summary_var, justify="left").pack(anchor="w")
        self.ttk.Label(body, textvariable=progress_var).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 8))

        controls = self.ttk.Frame(body); controls.grid(row=5, column=0, columnspan=3, sticky="e")
        create_button = self.ttk.Button(controls, text="CREATE")
        cancel_button = self.ttk.Button(controls, text="CANCEL", command=dialog.destroy)
        create_button.pack(side="left", padx=4); cancel_button.pack(side="left", padx=4)

        def refresh_summary() -> None:
            folder = folder_var.get().strip()
            if not folder:
                summary_var.set("เลือก Project Folder เพื่อดูรายละเอียด"); return
            try:
                path = Path(folder).expanduser()
                suggested = sanitize_project_name(path.name)
                if not name_var.get().strip() or name_var.get().strip() == auto_name["value"]:
                    auto_name["value"] = suggested; name_var.set(suggested)
                preview = preview_project(folder, name_var.get())
                summary_var.set(
                    f"Project: {preview.name}\nFolder: {preview.project_path}\n"
                    f"Serena Folder: {preview.launcher_dir}\nTunnel Profile: {preview.profile_name}\n"
                    f"Tunnel ID: {tunnel_var.get().strip() or '(required)'}\n"
                    f"MCP Port: {preview.mcp_port}\nHealth Port: {preview.health_port}\n"
                    f"Language: {preview.language_label}"
                )
            except Exception as exc:
                summary_var.set(f"ตรวจสอบไม่ได้: {exc}")

        def browse_folder() -> None:
            selected = filedialog.askdirectory(parent=dialog, title="Select project folder")
            if not selected: return
            folder_var.set(selected)
            try:
                auto_name["value"] = sanitize_project_name(Path(selected).name)
                name_var.set(auto_name["value"])
            except ValueError:
                name_var.set("")
            refresh_summary()

        browse_button = self.ttk.Button(body, text="Browse...", command=browse_folder)
        browse_button.grid(row=0, column=2, padx=(6, 0), pady=4)
        folder_entry.bind("<FocusOut>", lambda _event: refresh_summary())
        name_entry.bind("<KeyRelease>", lambda _event: refresh_summary())
        tunnel_entry.bind("<KeyRelease>", lambda _event: refresh_summary())

        def begin_create() -> None:
            try:
                folder = folder_var.get().strip()
                if not folder: raise ValueError("Project Folder is required")
                name = sanitize_project_name(name_var.get() or Path(folder).name)
                tunnel_id = validate_tunnel_id(tunnel_var.get())
                preview_project(folder, name)
            except Exception as exc:
                self.messagebox.showerror("Add Serena Project", str(exc), parent=dialog); return

            self.busy = True
            create_button.configure(state="disabled"); cancel_button.configure(state="disabled")
            browse_button.configure(state="disabled"); folder_entry.configure(state="disabled")
            name_entry.configure(state="disabled"); tunnel_entry.configure(state="disabled")
            events: queue.Queue[tuple[str, object]] = queue.Queue()

            def report(message: str) -> None: events.put(("progress", message))
            def work() -> None:
                try:
                    result = create_project_setup(folder, name, tunnel_id, report)
                    events.put(("success", result))
                except Exception as exc:
                    detail = traceback.format_exc()
                    try:
                        ADD_ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
                        with ADD_ERROR_LOG.open("a", encoding="utf-8") as handle:
                            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ADD {name} failed\n{detail}\n")
                    except OSError:
                        pass
                    events.put(("error", f"{exc}\n\nDetailed log: {ADD_ERROR_LOG}"))
            def poll() -> None:
                done = False
                while True:
                    try: event, value = events.get_nowait()
                    except queue.Empty: break
                    if event == "progress":
                        progress_var.set(str(value)); self.status_var.set(str(value))
                    elif event == "error":
                        done = True; self.busy = False; self.status_var.set("Create failed")
                        self.messagebox.showerror("Add Serena Project", str(value), parent=dialog)
                        create_button.configure(state="normal"); cancel_button.configure(state="normal")
                        browse_button.configure(state="normal"); folder_entry.configure(state="normal")
                        name_entry.configure(state="normal"); tunnel_entry.configure(state="normal")
                    elif event == "success":
                        done = True
                        projects = order_projects(discover_projects()); save_project_order(projects)
                        self.busy = False; dialog.destroy()
                        self.messagebox.showinfo("Add Serena Project", f"Created {value.preview.name}\nPorts: {value.preview.mcp_port}/{value.preview.health_port}")
                        self.refresh()
                if not done: self.root.after(100, poll)

            threading.Thread(target=work, daemon=True).start()
            self.root.after(100, poll)

        create_button.configure(command=begin_create)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None if self.busy else dialog.destroy())
        folder_entry.focus_set()

    def open_stale_entry_remove_dialog(self, project: Project) -> None:
        dialog = self.tk.Toplevel(self.root)
        dialog.title("Remove Stale Serena Entry")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        body = self.ttk.Frame(dialog, padding=16); body.pack(fill="both", expand=True)
        details = (
            "Project folder was not found.\n\n"
            f"Project: {project.name}\n"
            f"Project Path: {project.project_path}\n\n"
            "Remove this stale entry from Serena Manager only?\n\n"
            "No source files, launcher files, tunnel profiles, or credentials will be deleted."
        )
        self.ttk.Label(body, text=details, justify="left").pack(anchor="w")
        controls = self.ttk.Frame(body); controls.pack(anchor="e", pady=(12, 0))
        remove_button = self.ttk.Button(controls, text="REMOVE ENTRY")
        cancel_button = self.ttk.Button(controls, text="CANCEL", command=dialog.destroy)
        remove_button.pack(side="left", padx=4); cancel_button.pack(side="left", padx=4)

        def begin_remove_entry() -> None:
            self.busy = True
            remove_button.configure(state="disabled"); cancel_button.configure(state="disabled")
            self.status_var.set(f"Removing stale entry {project.name}...")
            events: queue.Queue[tuple[str, object]] = queue.Queue()
            def work() -> None:
                try:
                    remove_stale_entry(project)
                    events.put(("success", None))
                except Exception as exc:
                    events.put(("error", str(exc)))
            def poll() -> None:
                try:
                    event, value = events.get_nowait()
                except queue.Empty:
                    self.root.after(100, poll); return
                if event == "error":
                    self.busy = False; self.status_var.set("Remove stale entry failed")
                    self.messagebox.showerror("Remove Serena Project", str(value), parent=dialog)
                    remove_button.configure(state="normal"); cancel_button.configure(state="normal")
                    return
                self.projects = [item for item in self.projects if item.name != project.name]
                self.states.pop(project.name, None); self._render()
                self.busy = False; dialog.destroy()
                self.messagebox.showinfo("Remove Serena Project", "Removed the stale Serena Manager entry only.")
                self.refresh()
            threading.Thread(target=work, daemon=True).start()
            self.root.after(100, poll)

        remove_button.configure(command=begin_remove_entry)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None if self.busy else dialog.destroy())
        cancel_button.focus_set()
    def open_remove_dialog(self) -> None:
        if self.busy: return
        project = self.selected_project()
        if not project: return
        if project.parse_error:
            self.messagebox.showerror("Remove Serena Project", f"REMOVE BLOCKED — {project.parse_error}"); return
        try:
            managed = verify_managed_project(
                project.name, project.project_path, project.folder, project.profile,
                project.mcp_port, project.health_port,
            )
        except OwnershipError as exc:
            if "manifest missing" not in str(exc):
                self.messagebox.showerror("Remove Serena Project", str(exc)); return
            try:
                verify_stale_entry_removal(project)
            except OwnershipError:
                self.messagebox.showerror("Remove Serena Project", str(exc)); return
            except Exception as stale_exc:
                self.messagebox.showerror("Remove Serena Project", f"REMOVE BLOCKED — stale entry check failed: {stale_exc}"); return
            self.open_stale_entry_remove_dialog(project)
            return
        except Exception as exc:
            self.messagebox.showerror("Remove Serena Project", f"REMOVE BLOCKED — {exc}"); return

        dialog = self.tk.Toplevel(self.root)
        dialog.title("Remove Serena Project")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()
        progress_var = self.tk.StringVar(value="Ready")
        body = self.ttk.Frame(dialog, padding=16); body.pack(fill="both", expand=True)
        details = (
            f"Project:\n{managed.name}\n\n"
            f"Project Path:\n{managed.project_path}\n\n"
            f"Serena Folder:\n{managed.launcher_dir}\n\n"
            f"Tunnel Profile:\n{managed.profile_name}\n\n"
            "This removes only the Serena integration.\n"
            "Your project source folder will NOT be deleted."
        )
        self.ttk.Label(body, text=details, justify="left").pack(anchor="w")
        self.ttk.Label(body, textvariable=progress_var).pack(anchor="w", pady=(12, 8))
        controls = self.ttk.Frame(body); controls.pack(anchor="e")
        remove_button = self.ttk.Button(controls, text="REMOVE")
        cancel_button = self.ttk.Button(controls, text="CANCEL", command=dialog.destroy)
        remove_button.pack(side="left", padx=4); cancel_button.pack(side="left", padx=4)

        def begin_remove() -> None:
            self.busy = True
            remove_button.configure(state="disabled"); cancel_button.configure(state="disabled")
            events: queue.Queue[tuple[str, object]] = queue.Queue()
            def report(message: str) -> None: events.put(("progress", message))
            def work() -> None:
                try:
                    result = remove_managed_project(
                        project.name, project.project_path, project.folder, project.profile,
                        project.mcp_port, project.health_port, ORDER_FILE, report,
                    )
                    events.put(("success", result))
                except Exception as exc:
                    events.put(("error", str(exc)))
            def poll() -> None:
                done = False
                while True:
                    try: event, value = events.get_nowait()
                    except queue.Empty: break
                    if event == "progress":
                        progress_var.set(str(value)); self.status_var.set(str(value))
                    elif event == "error":
                        done = True; self.busy = False; self.status_var.set("Remove failed")
                        self.messagebox.showerror("Remove Serena Project", str(value), parent=dialog)
                        remove_button.configure(state="normal"); cancel_button.configure(state="normal")
                    elif event == "success":
                        done = True
                        self.projects = [item for item in self.projects if item.name != project.name]
                        self.states.pop(project.name, None); self._render()
                        self.busy = False; dialog.destroy()
                        self.messagebox.showinfo(
                            "Remove Serena Project",
                            f"Removed Serena integration for {value.name}.\nProject source was preserved.",
                        )
                        self.refresh()
                if not done: self.root.after(100, poll)
            threading.Thread(target=work, daemon=True).start()
            self.root.after(100, poll)

        remove_button.configure(command=begin_remove)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None if self.busy else dialog.destroy())
        cancel_button.focus_set()

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for project in self.projects:
            state = self.states.get(project.name); status = state.status if state else "ERROR"
            self.tree.insert("", "end", iid=project.name, values=(project.name, project.project_path or project.parse_error,
                state.serena.state if state else "ERROR", state.tunnel.state if state else "ERROR",
                project.mcp_port or "-", project.health_port or "-", status), tags=(status,))

    def refresh(self) -> None:
        if self.busy: return
        self.busy = True; self.status_var.set("Refreshing...")
        results: queue.Queue[tuple[list[Project], dict[str, ProjectState]]] = queue.Queue()
        def work() -> None:
            projects = order_projects(discover_projects()); states = get_project_states(projects); results.put((projects, states))
        def poll() -> None:
            try: projects, states = results.get_nowait()
            except queue.Empty: self.root.after(100, poll); return
            self.projects, self.states = projects, states; self._render(); self.status_var.set(f"Discovered {len(projects)} projects"); self.busy = False
        threading.Thread(target=work, daemon=True).start(); self.root.after(100, poll)

    def _run_project_launcher(self, project: Project, path: Path | None, action: str) -> None:
        if self.busy: return
        if project.parse_error or path is None or not path.is_file():
            self.messagebox.showerror("Serena Manager", project.parse_error or f"Missing {action} launcher"); return
        self.busy = True; self.status_var.set(f"{action} {project.name}...")
        results: queue.Queue[tuple[ProjectState, str]] = queue.Queue()
        def work() -> None:
            try:
                launcher_process = start_launcher(path)
                deadline = time.monotonic() + 45.0
                desired = "RUNNING" if action in ("Starting", "Restarting") else ("RUNNING" if action == "Debugging" else "STOPPED")
                state = get_project_state(project)
                while state.status != desired and time.monotonic() < deadline:
                    time.sleep(0.25)
                    state = get_project_state(project)
                if state.status != desired and time.monotonic() >= deadline:
                    message = f"Timed out waiting for {desired}. Serena={state.serena.state} ({state.serena.detail}); Tunnel={state.tunnel.state} ({state.tunnel.detail})"
                else:
                    message = f"Serena={state.serena.state}; Tunnel={state.tunnel.state}"
                if launcher_process.poll() is not None and state.status != desired:
                    message += f"; launcher exit={launcher_process.returncode}"
            except Exception as exc:
                state = ProjectState(ServiceState("ERROR",str(exc)),ServiceState("ERROR",str(exc)),"ERROR"); message = str(exc)
            results.put((state, message))
        def poll() -> None:
            try: state, message = results.get_nowait()
            except queue.Empty: self.root.after(100, poll); return
            self.states[project.name]=state; self._render(); self.status_var.set(f"{project.name}: {state.status}"); self.busy=False
            if state.status == "ERROR": self.messagebox.showerror("Serena Manager", message or "Launcher failed")
        threading.Thread(target=work, daemon=True).start(); self.root.after(100, poll)

    def start_selected(self) -> None:
        project=self.selected_project()
        if project: self._run_project_launcher(project, project.start_cmd, "Starting")
    def stop_selected(self) -> None:
        project=self.selected_project()
        if project: self._run_project_launcher(project, project.stop_cmd, "Stopping")
    def restart_selected(self) -> None:
        project=self.selected_project()
        if project: self._run_project_launcher(project, project.restart_cmd, "Restarting")
    def debug_selected(self) -> None:
        project=self.selected_project()
        if project: self._run_project_launcher(project, project.debug_cmd, "Debugging")
    def open_log(self) -> None:
        project=self.selected_project()
        if not project: return
        if not project.log_file:
            self.messagebox.showerror("Serena Manager", "Log path is unavailable."); return
        project.log_file.parent.mkdir(parents=True, exist_ok=True)
        project.log_file.touch(exist_ok=True)
        subprocess.Popen(["notepad.exe", str(project.log_file)], creationflags=CREATE_NO_WINDOW)
    def stop_all(self) -> None:
        if self.busy: return
        targets=[p for p in self.projects if self.states.get(p.name) and self.states[p.name].status in ("RUNNING","PARTIAL")]
        if not targets: self.status_var.set("No running projects"); return
        self.busy=True; self.status_var.set(f"Stopping {len(targets)} projects...")
        results: queue.Queue[tuple[list[Project], dict[str, ProjectState]]] = queue.Queue()
        def work() -> None:
            for project in targets:
                if project.stop_cmd and project.stop_cmd.is_file(): run_launcher(project.stop_cmd)
            projects=discover_projects(); states={p.name:get_project_state(p) for p in projects}
            results.put((projects, states))
        def poll() -> None:
            try: projects, states = results.get_nowait()
            except queue.Empty: self.root.after(100, poll); return
            self.projects,self.states=projects,states; self._render(); self.status_var.set("Stop All finished"); self.busy=False
        threading.Thread(target=work,daemon=True).start(); self.root.after(100,poll)
    def inspect_selected(self, _event: object=None) -> None:
        project=self.selected_project()
        if not project: return
        state=self.states.get(project.name)
        if state: self.messagebox.showinfo(project.name, f"Project path: {project.project_path}\nMCP port: {project.mcp_port}\nHealth port: {project.health_port}\nProfile: {project.profile}\nSerena: {state.serena.state} - {state.serena.detail}\nTunnel: {state.tunnel.state} - {state.tunnel.detail}")
    def run(self) -> None: self.root.mainloop()

def main() -> int:
    parser=argparse.ArgumentParser(description="Manage local Serena MCP launchers")
    parser.add_argument("--self-test",action="store_true"); parser.add_argument("--logic-test",action="store_true"); args=parser.parse_args()
    if args.logic_test: logic_test(); print("LOGIC_TEST=PASS"); return 0
    if args.self_test:
        projects=discover_projects(); payload=[]
        for project in projects:
            state=get_project_state(project); item=asdict(project); item.update({"folder":str(project.folder),"start_cmd":str(project.start_cmd or ""),"stop_cmd":str(project.stop_cmd or ""),"state":asdict(state)}); payload.append(item)
        print(json.dumps(payload,ensure_ascii=False,indent=2,default=str)); return 0 if projects and all(not p.parse_error for p in projects) else 1
    ManagerApp().run(); return 0

if __name__ == "__main__": raise SystemExit(main())
