from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MANAGER_ROOT = Path(__file__).resolve().parent
# Keep all machine-specific locations configurable.  The default preserves the
# existing sibling-launcher layout without embedding a Windows account name.
TOOLS_ROOT = Path(os.environ.get("SERENA_TOOLS_ROOT", str(MANAGER_ROOT.parent))).expanduser()
PROFILE_DIR = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "tunnel-client"
CREDENTIAL_FILE = TOOLS_ROOT / "control-plane-api-key.dpapi"
TUNNEL_CLIENT = TOOLS_ROOT / "tunnel-client-v0.0.14-windows-amd64" / "tunnel-client.exe"
SHARED_LAUNCHER = MANAGER_ROOT / "Serena-Launcher.ps1"
STOP_HELPER = MANAGER_ROOT / "Stop-SerenaProject.ps1"
SERENA_GLOBAL_CONFIG = Path.home() / ".serena" / "serena_config.yml"
MANIFEST_NAME = "serena-manager-project.json"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

PORT_ASSIGNMENT = re.compile(r"^\s*\$(?:serenaPort|tunnelPort)\s*=\s*(\d+)\s*$", re.MULTILINE)
SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")
SAFE_TUNNEL = re.compile(r"^tunnel_[A-Za-z0-9_-]+$")
IGNORED_SCAN_DIRS = {".git", ".serena", "node_modules", "vendor", ".venv", "venv", "__pycache__"}


@dataclass(frozen=True)
class ProjectPreview:
    name: str
    project_path: Path
    launcher_dir: Path
    profile_name: str
    profile_path: Path
    mcp_port: int
    health_port: int
    language_label: str
    language_servers: tuple[str, ...]
    empty: bool


@dataclass(frozen=True)
class CreateResult:
    preview: ProjectPreview
    runtime_tested: bool


@dataclass(frozen=True)
class ManagedProject:
    name: str
    project_path: Path
    launcher_dir: Path
    profile_name: str
    profile_path: Path
    mcp_port: int
    health_port: int
    created_serena_metadata: bool
    created_serena_registration: bool
    managed_files: dict[str, str]
    profile_sha256: str
    project_config_sha256: str


class OwnershipError(RuntimeError):
    pass


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(str(_resolved(left))) == os.path.normcase(str(_resolved(right)))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _registered_projects(config_path: Path = SERENA_GLOBAL_CONFIG) -> list[Path]:
    try:
        lines = config_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return []
    in_projects = False
    result: list[Path] = []
    for line in lines:
        if not in_projects:
            if line.strip() == "projects:" and not line.startswith((" ", "\t")):
                in_projects = True
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^-\s+(.+?)\s*$", line)
        if not match:
            break
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result.append(_resolved(value))
    return result


def _is_registered(project_path: Path, config_path: Path = SERENA_GLOBAL_CONFIG) -> bool:
    return any(_same_path(item, project_path) for item in _registered_projects(config_path))


def _remove_registration(project_path: Path, config_path: Path = SERENA_GLOBAL_CONFIG) -> bool:
    if not config_path.is_file():
        return False
    original = config_path.read_text(encoding="utf-8-sig")
    lines = original.splitlines(keepends=True)
    in_projects = False
    removed = False
    output: list[str] = []
    for line in lines:
        if not in_projects:
            output.append(line)
            if line.strip() == "projects:" and not line.startswith((" ", "\t")):
                in_projects = True
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            output.append(line)
            continue
        match = re.match(r"^-\s+(.+?)\s*(?:\r?\n)?$", line)
        if not match:
            in_projects = False
            output.append(line)
            continue
        value = match.group(1).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not removed and _same_path(value, project_path):
            removed = True
            continue
        output.append(line)
    if removed:
        temporary = config_path.with_name(config_path.name + ".serena-manager.tmp")
        temporary.write_text("".join(output), encoding="utf-8", newline="")
        os.replace(temporary, config_path)
    return removed


def sanitize_project_name(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("Project name is required")
    if ".." in raw or any(char in raw for char in "\\/:*"):
        raise ValueError("Project name contains a forbidden path character")
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-_").lower()
    slug = re.sub(r"-+", "-", slug)
    if not slug or not SAFE_NAME.fullmatch(slug):
        raise ValueError("Project name must contain a-z, 0-9, - or _")
    return slug


def validate_tunnel_id(value: str) -> str:
    tunnel_id = value.strip()
    if not tunnel_id:
        raise ValueError("Tunnel ID is required")
    if not SAFE_TUNNEL.fullmatch(tunnel_id):
        raise ValueError("Tunnel ID must start with tunnel_ and contain only letters, numbers, - or _")
    return tunnel_id


def _scan_project(project_path: Path) -> tuple[set[str], bool]:
    markers: set[str] = set()
    has_files = False
    scanned = 0
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [name for name in dirs if name not in IGNORED_SCAN_DIRS]
        for filename in files:
            has_files = True
            scanned += 1
            lower = filename.lower()
            suffix = Path(lower).suffix
            if lower == "composer.json" or suffix == ".php": markers.add("PHP")
            if lower in {"pyproject.toml", "requirements.txt", "setup.py"} or suffix == ".py": markers.add("Python")
            if lower == "tsconfig.json" or suffix in {".ts", ".tsx"}: markers.add("TypeScript")
            if lower == "package.json" or suffix in {".js", ".jsx", ".mjs", ".cjs"}: markers.add("JavaScript")
            if scanned >= 5000:
                return markers, has_files
    return markers, has_files


def detect_languages(project_path: Path) -> tuple[str, tuple[str, ...], bool]:
    markers, has_files = _scan_project(project_path)
    ordered = [name for name in ("PHP", "Python", "TypeScript", "JavaScript") if name in markers]
    servers: list[str] = []
    for label in ordered:
        server = {"PHP": "php", "Python": "python", "TypeScript": "typescript", "JavaScript": "typescript"}[label]
        if server not in servers: servers.append(server)
    if not has_files:
        return "EMPTY", (), True
    if not ordered:
        return "Generic", (), False
    return " + ".join(ordered), tuple(servers), False


def _port_free(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_ports(tools_root: Path = TOOLS_ROOT) -> tuple[int, int]:
    used: set[int] = set()
    for launcher in tools_root.glob("Serena-*/Start-Serena-*.ps1"):
        try:
            used.update(int(value) for value in PORT_ASSIGNMENT.findall(launcher.read_text(encoding="utf-8-sig")))
        except OSError:
            continue
    for mcp_port in range(8000, 8100):
        health_port = mcp_port + 80
        if mcp_port in used or health_port in used:
            continue
        if _port_free(mcp_port) and _port_free(health_port):
            return mcp_port, health_port
    raise RuntimeError("No free MCP/health port pair is available in 8000-8099")


def preview_project(folder: str | Path, project_name: str, tools_root: Path = TOOLS_ROOT,
                    profile_dir: Path = PROFILE_DIR) -> ProjectPreview:
    project_path = Path(folder).expanduser().resolve(strict=True)
    if not project_path.is_dir():
        raise ValueError("Project folder does not exist")
    name = sanitize_project_name(project_name or project_path.name)
    launcher_dir = tools_root / f"Serena-{name}"
    profile_name = f"serena-{name}"
    profile_path = profile_dir / f"{profile_name}.yaml"
    if launcher_dir.exists():
        raise FileExistsError(f"Serena project already exists: {launcher_dir}")
    if profile_path.exists():
        raise FileExistsError(f"Tunnel profile already exists: {profile_path}")
    if (project_path / ".serena").exists():
        raise FileExistsError(f"Serena metadata already exists: {project_path / '.serena'}")
    mcp_port, health_port = allocate_ports(tools_root)
    language_label, language_servers, empty = detect_languages(project_path)
    return ProjectPreview(name, project_path, launcher_dir, profile_name, profile_path,
                          mcp_port, health_port, language_label, language_servers, empty)


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _serena_config(preview: ProjectPreview) -> str:
    language_lines = "[]" if not preview.language_servers else "\n" + "\n".join(f"- {name}" for name in preview.language_servers)
    tools = ["read_file", "list_dir", "find_file", "search_for_pattern", "create_text_file",
             "replace_content", "replace_in_files", "execute_shell_command", "get_current_config"]
    if preview.language_servers:
        tools.extend(["get_symbols_overview", "find_symbol", "find_referencing_symbols",
                      "replace_symbol_body", "insert_after_symbol", "insert_before_symbol"])
    tool_lines = "\n".join(f"- {name}" for name in tools)
    prompt = "This is an intentionally empty project. No language server is configured until real source files establish the language." if preview.empty else ""
    return (
        f"project_name: {json.dumps(preview.name)}\n"
        f"language_servers: {language_lines}\n"
        "encoding: \"utf-8\"\n"
        "ignore_all_files_in_gitignore: true\n"
        "ls_workspace_folders:\n- \".\"\n"
        "ls_additional_workspace_folders: []\n"
        "ignored_paths: []\n"
        "read_only: false\n"
        "excluded_tools: []\n"
        "included_optional_tools: []\n"
        f"fixed_tools:\n{tool_lines}\n"
        "default_modes:\n- interactive\n- editing\n"
        "added_modes: []\n"
        f"initial_prompt: {json.dumps(prompt)}\n"
    )


def _write_launchers(preview: ProjectPreview, tunnel_client: Path, credential_file: Path,
                     shared_launcher: Path, stop_helper: Path) -> None:
    name = preview.name
    folder = preview.launcher_dir
    start_ps1 = (
        "param([switch]$DebugMode)\n"
        f"$project = '{_ps_quote(str(preview.project_path))}'\n"
        f"$tunnelClient = '{_ps_quote(str(tunnel_client))}'\n"
        f"$profile = '{_ps_quote(preview.profile_name)}'\n"
        f"$credentialFile = '{_ps_quote(str(credential_file))}'\n"
        f"$serenaPort = {preview.mcp_port}\n"
        f"$tunnelPort = {preview.health_port}\n"
        "$extra = ''\n"
        f"& (Join-Path $PSScriptRoot 'Serena-Launcher.ps1') -Project $project -TunnelClient $tunnelClient -Profile $profile -CredentialFile $credentialFile -SerenaPort $serenaPort -TunnelPort $tunnelPort -Label '{name}' -SerenaExtraArgs $extra -DebugMode:$DebugMode\n"
        "exit $LASTEXITCODE\n"
    )
    stop_ps1 = (
        "$ErrorActionPreference = 'Stop'\n"
        "$logDir = Join-Path $PSScriptRoot 'logs'; New-Item -ItemType Directory -Force -Path $logDir | Out-Null\n"
        f"$label = '{name}'\n"
        "Add-Content -LiteralPath (Join-Path $logDir 'launcher.log') -Value (\"{0:o} [$label] STOP requested\" -f (Get-Date)) -Encoding UTF8\n"
        f"& '{_ps_quote(str(stop_helper))}' -Project '{_ps_quote(str(preview.project_path))}' -Profile '{preview.profile_name}' -SerenaPort {preview.mcp_port} -TunnelPort {preview.health_port}\n"
        "$code = $LASTEXITCODE\n"
        "Add-Content -LiteralPath (Join-Path $logDir 'launcher.log') -Value (\"{0:o} [$label] STOP finished code={1}\" -f (Get-Date), $code) -Encoding UTF8\n"
        "exit $code\n"
    )
    start_cmd = f'@echo off\r\nsetlocal\r\nwscript.exe //nologo "%~dp0Start-Serena-{name}-Hidden.vbs"\r\nexit /b 0\r\n'
    hidden_vbs = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'folder = fso.GetParentFolderName(WScript.ScriptFullName)\r\n'
        f'command = "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & folder & "\\Start-Serena-{name}.ps1"""\r\n'
        'shell.Run command, 0, False\r\n'
    )
    debug_cmd = f'@echo off\r\nsetlocal\r\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Serena-{name}.ps1" -DebugMode\r\nexit /b %ERRORLEVEL%\r\n'
    stop_cmd = f'@echo off\r\nsetlocal\r\npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-Serena-{name}.ps1"\r\nexit /b %ERRORLEVEL%\r\n'
    restart_cmd = f'@echo off\r\nsetlocal\r\ncall "%~dp0Stop-Serena-{name}.cmd"\r\ntimeout /t 2 /nobreak >nul\r\ncall "%~dp0Start-Serena-{name}.cmd"\r\nexit /b %ERRORLEVEL%\r\n'
    readme = (
        f"Serena {name} launcher\n\nProject: {preview.project_path}\n"
        f"Serena MCP: http://127.0.0.1:{preview.mcp_port}/mcp\n"
        f"tunnel-client health/UI: http://127.0.0.1:{preview.health_port}\n"
        f"Tunnel profile: {preview.profile_name}\nCredential: {credential_file}\n"
    )
    files = {
        f"Start-Serena-{name}.ps1": start_ps1,
        f"Stop-Serena-{name}.ps1": stop_ps1,
        f"Start-Serena-{name}.cmd": start_cmd,
        f"Start-Serena-{name}-Hidden.vbs": hidden_vbs,
        f"Start-Serena-{name}-Debug.cmd": debug_cmd,
        f"Stop-Serena-{name}.cmd": stop_cmd,
        f"Restart-Serena-{name}.cmd": restart_cmd,
        "README.txt": readme,
    }
    for filename, content in files.items():
        (folder / filename).write_text(content, encoding="utf-8", newline="")
    shutil.copy2(shared_launcher, folder / "Serena-Launcher.ps1")
    (folder / "logs").mkdir()


def _write_profile(preview: ProjectPreview, tunnel_id: str) -> None:
    content = (
        "config_version: 1\ncontrol_plane:\n"
        "  base_url: \"https://api.openai.com\"\n"
        f"  tunnel_id: {json.dumps(tunnel_id)}\n"
        "  api_key: \"env:CONTROL_PLANE_API_KEY\"\n"
        f"health:\n  listen_addr: \"127.0.0.1:{preview.health_port}\"\n"
        "admin_ui:\n  open_browser: false\n"
        "log:\n  level: info\n  format: json\n"
        "mcp:\n  server_urls:\n    - channel: main\n"
        f"      url: \"http://127.0.0.1:{preview.mcp_port}/mcp\"\n"
    )
    preview.profile_path.write_text(content, encoding="utf-8")


def _expected_launcher_files(name: str) -> set[str]:
    return {
        f"Start-Serena-{name}.ps1",
        f"Stop-Serena-{name}.ps1",
        f"Start-Serena-{name}.cmd",
        f"Start-Serena-{name}-Hidden.vbs",
        f"Start-Serena-{name}-Debug.cmd",
        f"Stop-Serena-{name}.cmd",
        f"Restart-Serena-{name}.cmd",
        "README.txt",
        "Serena-Launcher.ps1",
    }


def _write_manifest(preview: ProjectPreview, created_registration: bool) -> Path:
    managed_files: dict[str, str] = {}
    for relative in sorted(_expected_launcher_files(preview.name)):
        path = preview.launcher_dir / relative
        if not path.is_file():
            raise RuntimeError(f"Cannot create ownership manifest; missing managed file: {relative}")
        managed_files[relative] = _sha256(path)
    config = preview.project_path / ".serena" / "project.yml"
    payload = {
        "manifest_version": 1,
        "managed_by": "Serena Manager",
        "project_name": preview.name,
        "project_path": str(preview.project_path),
        "launcher_folder": str(preview.launcher_dir),
        "profile_name": preview.profile_name,
        "tunnel_profile": str(preview.profile_path),
        "mcp_port": preview.mcp_port,
        "health_port": preview.health_port,
        "created_serena_metadata": True,
        "created_serena_registration": bool(created_registration),
        "managed_files": managed_files,
        "profile_sha256": _sha256(preview.profile_path),
        "project_config_sha256": _sha256(config),
    }
    manifest_path = preview.launcher_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _allowed_log_file(relative: str) -> bool:
    return re.fullmatch(r"logs/(?:launcher|serena|tunnel|tunnel\.stderr)\.log(?:\.[12])?", relative) is not None


def verify_managed_project(project_name: str, project_path: str | Path, launcher_dir: str | Path,
                           profile_name: str, mcp_port: int, health_port: int, *,
                           tools_root: Path = TOOLS_ROOT, profile_dir: Path = PROFILE_DIR) -> ManagedProject:
    name = sanitize_project_name(project_name)
    source = _resolved(project_path)
    launcher = _resolved(launcher_dir)
    expected_launcher = _resolved(tools_root / f"Serena-{name}")
    profile = _resolved(profile_dir / f"{profile_name}.yaml")
    expected_profile = _resolved(profile_dir / f"serena-{name}.yaml")
    if launcher != expected_launcher or launcher.parent != _resolved(tools_root):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: launcher path mismatch")
    if profile_name != f"serena-{name}" or profile != expected_profile:
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: tunnel profile mismatch")
    if launcher.is_symlink() or getattr(launcher, "is_junction", lambda: False)():
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: launcher is a link/junction")
    manifest_path = launcher / MANIFEST_NAME
    if not manifest_path.is_file():
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: manifest missing")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise OwnershipError(f"REMOVE BLOCKED — ownership could not be verified: invalid manifest ({exc})") from exc
    exact = {
        "managed_by": "Serena Manager",
        "project_name": name,
        "profile_name": profile_name,
        "mcp_port": int(mcp_port),
        "health_port": int(health_port),
    }
    if payload.get("manifest_version") != 1 or any(payload.get(key) != value for key, value in exact.items()):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: manifest identity mismatch")
    if not _same_path(payload.get("project_path", ""), source):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: project path mismatch")
    if not _same_path(payload.get("launcher_folder", ""), launcher):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: manifest launcher mismatch")
    if not _same_path(payload.get("tunnel_profile", ""), profile):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: manifest profile mismatch")
    managed_files = payload.get("managed_files")
    if not isinstance(managed_files, dict) or set(managed_files) != _expected_launcher_files(name):
        raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: managed file inventory mismatch")
    for relative, expected_hash in managed_files.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise OwnershipError("REMOVE BLOCKED — ownership could not be verified: unsafe managed path")
        path = launcher / relative_path
        if not path.is_file() or _sha256(path) != str(expected_hash).upper():
            raise OwnershipError(f"REMOVE BLOCKED — managed file changed or missing: {relative}")
    for item in launcher.rglob("*"):
        relative = item.relative_to(launcher).as_posix()
        if item.is_dir():
            if relative != "logs":
                raise OwnershipError(f"REMOVE BLOCKED — unmanaged directory in launcher folder: {relative}")
        elif relative != MANIFEST_NAME and relative not in managed_files and not _allowed_log_file(relative):
            raise OwnershipError(f"REMOVE BLOCKED — unmanaged file in launcher folder: {relative}")
    if profile.is_file() and _sha256(profile) != str(payload.get("profile_sha256", "")).upper():
        raise OwnershipError("REMOVE BLOCKED — local tunnel profile was modified")
    created_metadata = payload.get("created_serena_metadata") is True
    config = source / ".serena" / "project.yml"
    serena_dir = source / ".serena"
    if not source.is_dir():
        raise OwnershipError("REMOVE BLOCKED — source project path is unavailable")
    if serena_dir.is_symlink() or getattr(serena_dir, "is_junction", lambda: False)():
        raise OwnershipError("REMOVE BLOCKED — Serena metadata is a link/junction")
    if created_metadata and serena_dir.exists() and not config.is_file():
        raise OwnershipError("REMOVE BLOCKED — Serena metadata identity file is missing")
    if created_metadata and config.is_file():
        config_text = config.read_text(encoding="utf-8-sig")
        project_match = re.search(r'^project_name:\s*["\']?([^"\'\r\n]+)["\']?\s*$', config_text, re.MULTILINE)
        if not project_match or project_match.group(1).strip() != name:
            raise OwnershipError("REMOVE BLOCKED — Serena metadata project identity mismatch")
    return ManagedProject(
        name, source, launcher, profile_name, profile, int(mcp_port), int(health_port),
        created_metadata, payload.get("created_serena_registration") is True,
        {str(key): str(value) for key, value in managed_files.items()},
        str(payload.get("profile_sha256", "")), str(payload.get("project_config_sha256", "")),
    )


def _remove_order_entry(order_file: Path, project_name: str) -> None:
    if not order_file.is_file():
        return
    value = json.loads(order_file.read_text(encoding="utf-8"))
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("Project order file is invalid")
    updated = [item for item in value if item != project_name]
    temporary = order_file.with_name(order_file.name + ".serena-manager.tmp")
    temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, order_file)


def remove_managed_project(project_name: str, project_path: str | Path, launcher_dir: str | Path,
                           profile_name: str, mcp_port: int, health_port: int, order_file: Path,
                           progress: Callable[[str], None] | None = None, *,
                           tools_root: Path = TOOLS_ROOT, profile_dir: Path = PROFILE_DIR,
                           global_config: Path = SERENA_GLOBAL_CONFIG,
                           _fail_after: str = "") -> ManagedProject:
    report = progress or (lambda _message: None)
    report("Verifying ownership...")
    managed = verify_managed_project(project_name, project_path, launcher_dir, profile_name,
                                     mcp_port, health_port, tools_root=tools_root, profile_dir=profile_dir)
    stop_cmd = managed.launcher_dir / f"Stop-Serena-{managed.name}.cmd"
    report("Stopping Serena and tunnel...")
    stopped = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(stop_cmd)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=75, creationflags=CREATE_NO_WINDOW, check=False)
    if stopped.returncode != 0:
        detail = (stopped.stdout + "\n" + stopped.stderr).strip()
        raise RuntimeError(f"REMOVE ABORTED — safe stop failed: {detail or 'unknown stop error'}")
    if not _port_free(managed.mcp_port) or not _port_free(managed.health_port):
        raise RuntimeError("REMOVE ABORTED — MCP or health port is still in use")

    serena_dir = managed.project_path / ".serena"
    with tempfile.TemporaryDirectory(prefix="serena-remove-backup-") as temporary_name:
        backup = Path(temporary_name)
        shutil.copytree(managed.launcher_dir, backup / "launcher")
        if managed.profile_path.is_file():
            shutil.copy2(managed.profile_path, backup / "profile.yaml")
        if managed.created_serena_metadata and serena_dir.is_dir():
            shutil.copytree(serena_dir, backup / "serena")
        if managed.created_serena_registration and global_config.is_file():
            shutil.copy2(global_config, backup / "serena_config.yml")
        if order_file.is_file():
            shutil.copy2(order_file, backup / "project-order.json")

        try:
            report("Removing local tunnel profile...")
            if managed.profile_path.exists():
                managed.profile_path.unlink()
            if _fail_after == "profile": raise RuntimeError("forced failure after profile removal")

            report("Removing Serena registration...")
            if managed.created_serena_registration:
                _remove_registration(managed.project_path, global_config)
            if _fail_after == "registration": raise RuntimeError("forced failure after registration removal")

            report("Removing managed Serena metadata...")
            if managed.created_serena_metadata and serena_dir.exists():
                shutil.rmtree(serena_dir)
            if _fail_after == "metadata": raise RuntimeError("forced failure after metadata removal")

            report("Cleaning project order...")
            _remove_order_entry(order_file, managed.name)
            if _fail_after == "order": raise RuntimeError("forced failure after order cleanup")

            report("Removing Serena launcher folder...")
            shutil.rmtree(managed.launcher_dir)
            if _fail_after == "launcher": raise RuntimeError("forced failure after launcher removal")
        except Exception as exc:
            restore_errors: list[str] = []
            try:
                if managed.launcher_dir.exists(): shutil.rmtree(managed.launcher_dir)
                shutil.copytree(backup / "launcher", managed.launcher_dir)
            except Exception as restore_exc: restore_errors.append(f"launcher: {restore_exc}")
            try:
                if (backup / "profile.yaml").is_file():
                    managed.profile_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup / "profile.yaml", managed.profile_path)
            except Exception as restore_exc: restore_errors.append(f"profile: {restore_exc}")
            try:
                if (backup / "serena").is_dir():
                    if serena_dir.exists(): shutil.rmtree(serena_dir)
                    shutil.copytree(backup / "serena", serena_dir)
            except Exception as restore_exc: restore_errors.append(f"metadata: {restore_exc}")
            try:
                if (backup / "serena_config.yml").is_file():
                    shutil.copy2(backup / "serena_config.yml", global_config)
            except Exception as restore_exc: restore_errors.append(f"registration: {restore_exc}")
            try:
                if (backup / "project-order.json").is_file():
                    order_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup / "project-order.json", order_file)
            except Exception as restore_exc: restore_errors.append(f"order: {restore_exc}")
            detail = f"; rollback errors: {', '.join(restore_errors)}" if restore_errors else "; rollback restored local artifacts"
            raise RuntimeError(f"REMOVE failed: {exc}{detail}") from exc
    report("Remove complete")
    return managed


def _parse_powershell(path: Path) -> None:
    command = f"$errors=$null;[System.Management.Automation.Language.Parser]::ParseFile('{_ps_quote(str(path))}',[ref]$null,[ref]$errors)|Out-Null;if($errors.Count){{$errors|ForEach-Object {{$_.Message}};exit 1}}"
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
                            capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError(f"PowerShell syntax invalid: {path.name}: {result.stdout or result.stderr}")


def validate_static(preview: ProjectPreview) -> None:
    config = preview.project_path / ".serena" / "project.yml"
    text = config.read_text(encoding="utf-8")
    if f'project_name: "{preview.name}"' not in text or "read_only: false" not in text:
        raise RuntimeError("Serena project config validation failed")
    _parse_powershell(preview.launcher_dir / f"Start-Serena-{preview.name}.ps1")
    _parse_powershell(preview.launcher_dir / f"Stop-Serena-{preview.name}.ps1")
    profile = preview.profile_path.read_text(encoding="utf-8")
    if f"127.0.0.1:{preview.health_port}" not in profile or f"127.0.0.1:{preview.mcp_port}/mcp" not in profile:
        raise RuntimeError("Tunnel profile validation failed")
    if not _port_free(preview.mcp_port) or not _port_free(preview.health_port):
        raise RuntimeError("Allocated port became busy before startup")


def _listening(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _health_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as response:
            return response.status == 200 and response.read().decode("utf-8", "replace").strip().lower() == "ready"
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _mcp_handshake(port: int) -> bool:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "serena-manager", "version": "1"}}}).encode("utf-8")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _listener_windows_hidden(ports: tuple[int, int]) -> bool:
    requested = ",".join(str(port) for port in ports)
    script = (f"$ports=@({requested});$rows=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|"
              "Where-Object {$ports -contains [int]$_.LocalPort}|ForEach-Object {"
              "$p=Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue;"
              "if($p){[PSCustomObject]@{port=$_.LocalPort;handle=[int64]$p.MainWindowHandle}}});"
              "if($rows.Count -eq 0){'[]'}else{ConvertTo-Json -Compress -InputObject $rows}")
    result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    if result.returncode != 0: return False
    rows = json.loads(result.stdout.strip() or "[]")
    if isinstance(rows, dict): rows = [rows]
    return len(rows) == 2 and all(int(row.get("handle", -1)) == 0 for row in rows)


def validate_runtime(preview: ProjectPreview, progress: Callable[[str], None]) -> None:
    start_cmd = preview.launcher_dir / f"Start-Serena-{preview.name}.cmd"
    stop_cmd = preview.launcher_dir / f"Stop-Serena-{preview.name}.cmd"
    progress("Testing Serena and tunnel...")
    subprocess.Popen([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(start_cmd)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if _listening(preview.mcp_port) and _mcp_handshake(preview.mcp_port) and _health_ready(preview.health_port):
                if not _listener_windows_hidden((preview.mcp_port, preview.health_port)):
                    raise RuntimeError("Background process has a visible window")
                return
            time.sleep(1)
        raise RuntimeError("Timed out waiting for MCP handshake and tunnel /readyz")
    finally:
        progress("Stopping validation processes...")
        subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(stop_cmd)],
                       capture_output=True, text=True, timeout=75, creationflags=CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (_listening(preview.mcp_port) or _listening(preview.health_port)):
            time.sleep(0.5)
        if _listening(preview.mcp_port) or _listening(preview.health_port):
            raise RuntimeError("Validation cleanup did not release allocated ports")


def rollback_created(preview: ProjectPreview, remove_registration: bool = False,
                     global_config: Path = SERENA_GLOBAL_CONFIG) -> None:
    if preview.profile_path.exists(): preview.profile_path.unlink()
    if preview.launcher_dir.exists(): shutil.rmtree(preview.launcher_dir)
    serena_dir = preview.project_path / ".serena"
    if serena_dir.exists(): shutil.rmtree(serena_dir)
    if remove_registration: _remove_registration(preview.project_path, global_config)


def create_project_setup(folder: str | Path, project_name: str, tunnel_id_value: str,
                         progress: Callable[[str], None] | None = None, *,
                         tools_root: Path = TOOLS_ROOT, profile_dir: Path = PROFILE_DIR,
                         credential_file: Path = CREDENTIAL_FILE, tunnel_client: Path = TUNNEL_CLIENT,
                         shared_launcher: Path = SHARED_LAUNCHER, stop_helper: Path = STOP_HELPER,
                         runtime_test: bool = True) -> CreateResult:
    report = progress or (lambda _message: None)
    report("Checking folder...")
    preview = preview_project(folder, project_name, tools_root, profile_dir)
    tunnel_id = validate_tunnel_id(tunnel_id_value)
    registration_preexisting = _is_registered(preview.project_path)
    for required in (credential_file, tunnel_client, shared_launcher, stop_helper):
        if not required.is_file(): raise FileNotFoundError(f"Required file not found: {required}")
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        report("Creating Serena config...")
        serena_dir = preview.project_path / ".serena"
        serena_dir.mkdir()
        (serena_dir / "project.yml").write_text(_serena_config(preview), encoding="utf-8")
        report("Creating launchers...")
        preview.launcher_dir.mkdir()
        _write_launchers(preview, tunnel_client, credential_file, shared_launcher, stop_helper)
        report("Creating tunnel profile...")
        _write_profile(preview, tunnel_id)
        report("Validating generated files...")
        validate_static(preview)
        if runtime_test:
            validate_runtime(preview, report)
        created_registration = not registration_preexisting and _is_registered(preview.project_path)
        report("Writing ownership manifest...")
        _write_manifest(preview, created_registration)
        report("Complete")
        return CreateResult(preview, runtime_test)
    except Exception:
        report("Rolling back created artifacts...")
        rollback_created(preview, not registration_preexisting and _is_registered(preview.project_path))
        raise
