from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .config import load_config
from .paths import get_paths
from .transfer import AUTO_BACKUP_FILENAMES, WatchlistFormat


GOOGLE_DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
GOOGLE_TOKEN_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
BUILTIN_GOOGLE_CLIENT_CONFIG_NAME = "_google_drive_oauth_client.json"
_CLOUD_TRANSFER_LOCK = threading.Lock()


class CloudBackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudBackupFile:
    file_id: str
    name: str
    modified_time: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class CloudBackupResult:
    files: tuple[CloudBackupFile, ...]


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _private_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def record_cloud_backup_status(
    *,
    success: bool,
    error: str | None = None,
    files: tuple[CloudBackupFile, ...] = (),
) -> Path:
    path = get_paths().cloud_backup_status_path
    previous = load_cloud_backup_status()
    payload: dict[str, Any] = {
        "last_attempt_at": _now_iso(),
        "success": success,
        "error": error,
        "files": [
            {
                "id": item.file_id,
                "name": item.name,
                "modified_time": item.modified_time,
                "size": item.size,
            }
            for item in files
        ],
    }
    if success:
        payload["last_success_at"] = payload["last_attempt_at"]
    elif previous.get("last_success_at"):
        payload["last_success_at"] = previous["last_success_at"]
    _private_json_write(path, payload)
    return path


def load_cloud_backup_status() -> dict[str, Any]:
    path = get_paths().cloud_backup_status_path
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class GoogleDriveBackupProvider:
    """Google Drive app-data provider using least-privilege OAuth access."""

    def __init__(
        self,
        *,
        client_config_path: Path | None = None,
        builtin_client_config_path: Path | None = None,
        token_path: Path | None = None,
        timeout_seconds: int | None = None,
    ) -> None:
        paths = get_paths()
        self.client_config_path = client_config_path or paths.google_drive_client_path
        self.builtin_client_config_path = (
            builtin_client_config_path
            if builtin_client_config_path is not None
            else Path(__file__).with_name(BUILTIN_GOOGLE_CLIENT_CONFIG_NAME)
            if client_config_path is None
            else None
        )
        self.token_path = token_path or paths.google_drive_token_path
        configured_timeout = load_config().cloud.google_drive_timeout_seconds
        self.timeout_seconds = max(5, int(timeout_seconds or configured_timeout))
        self._credential_lock = threading.Lock()

    @staticmethod
    def _google_auth_types():
        try:
            from google.auth.transport.requests import Request as GoogleRequest
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:  # pragma: no cover - exercised when installation is incomplete
            raise CloudBackupError(
                "Google Drive support is not installed. Re-run scripts/install-user.sh to install cloud dependencies."
            ) from exc
        return GoogleRequest, Credentials, InstalledAppFlow

    def install_client_config(self, source: str | Path) -> Path:
        source_path = Path(source).expanduser()
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CloudBackupError(f"Google OAuth credentials file not found: {source_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Could not read Google OAuth credentials: {exc}") from exc
        self._validate_client_config(payload)
        try:
            _private_json_write(self.client_config_path, payload)
        except OSError as exc:
            raise CloudBackupError(f"Could not store Google OAuth credentials: {exc}") from exc
        return self.client_config_path

    @staticmethod
    def _validate_client_config(payload: Any) -> None:
        installed = payload.get("installed") if isinstance(payload, dict) else None
        if not isinstance(installed, dict):
            raise CloudBackupError("Google OAuth credentials must be for a Desktop app client.")
        missing = [
            key
            for key in ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")
            if not installed.get(key)
        ]
        if missing:
            raise CloudBackupError(f"Google OAuth credentials are missing: {', '.join(missing)}")

    def _load_client_config(self) -> dict[str, Any]:
        config_path = self.resolved_client_config_path()
        if config_path is None:
            raise CloudBackupError(
                "This build does not include AniAutoWatchList's Google OAuth client configuration. "
                "The app maintainer must configure the Google integration before users can connect."
            )
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Could not read Google OAuth credentials: {exc}") from exc
        self._validate_client_config(payload)
        return payload

    def has_client_config(self) -> bool:
        return self.resolved_client_config_path() is not None

    def has_builtin_client_config(self) -> bool:
        return self.builtin_client_config_path is not None and self.builtin_client_config_path.is_file()

    def client_config_source(self) -> str | None:
        if self.client_config_path.is_file():
            return "custom"
        if self.has_builtin_client_config():
            return "built-in"
        return None

    def resolved_client_config_path(self) -> Path | None:
        if self.client_config_path.is_file():
            return self.client_config_path
        if self.has_builtin_client_config():
            return self.builtin_client_config_path
        return None

    def is_connected(self) -> bool:
        return self.token_path.exists()

    def test_connection(self) -> tuple[CloudBackupFile, ...]:
        """Refresh authorization if needed and make a read-only Drive API request."""
        if not self.is_connected():
            raise CloudBackupError("Google Drive is not connected.")
        return self.list_backups()

    def connect(self) -> None:
        _GoogleRequest, _Credentials, InstalledAppFlow = self._google_auth_types()
        client_config = self._load_client_config()
        try:
            flow = InstalledAppFlow.from_client_config(client_config, [GOOGLE_DRIVE_APPDATA_SCOPE])
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=True,
                authorization_prompt_message="Opening your browser to connect AniAutoWatchList to Google Drive...",
                success_message="Google Drive is connected. You can close this browser tab and return to AniAutoWatchList.",
                timeout_seconds=600,
            )
            self._save_credentials(credentials)
        except CloudBackupError:
            raise
        except Exception as exc:
            raise CloudBackupError(f"Google Drive sign-in failed: {exc}") from exc

    def _load_credentials(self):
        GoogleRequest, Credentials, _InstalledAppFlow = self._google_auth_types()
        try:
            payload = json.loads(self.token_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CloudBackupError("Google Drive is not connected.") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Could not read the Google Drive sign-in token: {exc}") from exc
        try:
            credentials = Credentials.from_authorized_user_info(payload, [GOOGLE_DRIVE_APPDATA_SCOPE])
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise CloudBackupError("Google Drive authorization expired. Connect Google Drive again.")
                credentials.refresh(GoogleRequest())
                self._save_credentials(credentials)
            return credentials
        except CloudBackupError:
            raise
        except Exception as exc:
            raise CloudBackupError(f"Google Drive authorization could not be refreshed: {exc}") from exc

    def _save_credentials(self, credentials: Any) -> None:
        try:
            payload = json.loads(credentials.to_json())
            _private_json_write(self.token_path, payload)
        except (OSError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Could not store the Google Drive sign-in token: {exc}") from exc

    def disconnect(self, *, revoke: bool = True) -> None:
        revoke_error: Exception | None = None
        if revoke and self.token_path.exists():
            try:
                credentials = self._load_credentials()
                token = credentials.refresh_token or credentials.token
                if token:
                    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
                    request = urllib.request.Request(
                        GOOGLE_TOKEN_REVOKE_URL,
                        data=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=self.timeout_seconds):
                        pass
            except Exception as exc:
                revoke_error = exc
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError as exc:
            raise CloudBackupError(f"Could not remove the local Google Drive token: {exc}") from exc
        if revoke_error is not None:
            raise CloudBackupError(
                f"Local Google Drive sign-in was removed, but Google access could not be revoked: {revoke_error}"
            ) from revoke_error

    def _authorized_request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        retry_auth: bool = True,
    ) -> bytes:
        with self._credential_lock:
            credentials = self._load_credentials()
        request_headers = {"Authorization": f"Bearer {credentials.token}", **dict(headers or {})}
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and retry_auth:
                try:
                    credentials.expiry = None
                    credentials.token = None
                    with self._credential_lock:
                        GoogleRequest, _Credentials, _InstalledAppFlow = self._google_auth_types()
                        credentials.refresh(GoogleRequest())
                        self._save_credentials(credentials)
                except Exception as refresh_exc:
                    raise CloudBackupError(f"Google Drive authorization refresh failed: {refresh_exc}") from refresh_exc
                return self._authorized_request(
                    url,
                    method=method,
                    body=body,
                    headers=headers,
                    retry_auth=False,
                )
            detail = self._http_error_detail(exc)
            raise CloudBackupError(f"Google Drive request failed: {detail}") from exc
        except urllib.error.URLError as exc:
            raise CloudBackupError(f"Google Drive request failed: {exc.reason}") from exc
        except OSError as exc:
            raise CloudBackupError(f"Google Drive request failed: {exc}") from exc

    @staticmethod
    def _http_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = ((payload.get("error") or {}).get("message") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            message = ""
        return f"HTTP {exc.code}: {message or exc.reason}"

    @staticmethod
    def _file_from_payload(payload: dict[str, Any]) -> CloudBackupFile:
        raw_size = payload.get("size")
        try:
            size = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size = None
        return CloudBackupFile(
            file_id=str(payload.get("id") or ""),
            name=str(payload.get("name") or ""),
            modified_time=str(payload.get("modifiedTime") or "") or None,
            size=size,
        )

    def list_backups(self) -> tuple[CloudBackupFile, ...]:
        names = tuple(AUTO_BACKUP_FILENAMES.values())
        name_query = " or ".join(f"name = '{name.replace(chr(39), chr(92) + chr(39))}'" for name in names)
        params = urllib.parse.urlencode(
            {
                "spaces": "appDataFolder",
                "q": f"trashed = false and ({name_query})",
                "fields": "files(id,name,modifiedTime,size)",
                "pageSize": "100",
            }
        )
        raw = self._authorized_request(f"{GOOGLE_DRIVE_API}/files?{params}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Google Drive returned an invalid file list: {exc}") from exc
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, list):
            raise CloudBackupError("Google Drive returned an invalid file list.")
        return tuple(self._file_from_payload(item) for item in files if isinstance(item, dict) and item.get("id"))

    def _latest_file(self, name: str, files: tuple[CloudBackupFile, ...] | None = None) -> CloudBackupFile | None:
        source = files if files is not None else self.list_backups()
        matches = [item for item in source if item.name == name]
        return max(matches, key=lambda item: item.modified_time or "") if matches else None

    def upload_backups(self, local_files: Mapping[WatchlistFormat, Path]) -> CloudBackupResult:
        with _CLOUD_TRANSFER_LOCK:
            remote_files = self.list_backups()
            uploaded: list[CloudBackupFile] = []
            for export_format in ("json", "xml"):
                path = Path(local_files[export_format])
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise CloudBackupError(f"Could not read local {export_format.upper()} backup: {exc}") from exc
                name = AUTO_BACKUP_FILENAMES[export_format]
                current = self._latest_file(name, remote_files)
                uploaded.append(
                    self._update_file(current.file_id, name, export_format, content)
                    if current is not None
                    else self._create_file(name, export_format, content)
                )
        return CloudBackupResult(files=tuple(uploaded))

    def _create_file(self, name: str, export_format: WatchlistFormat, content: bytes) -> CloudBackupFile:
        boundary = f"ani-watchlist-{secrets.token_hex(16)}"
        metadata = json.dumps({"name": name, "parents": ["appDataFolder"]}).encode("utf-8")
        mime_type = "application/json" if export_format == "json" else "application/xml"
        body = b"".join(
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode("ascii"),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode("ascii"),
                content,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            )
        )
        fields = urllib.parse.quote("id,name,modifiedTime,size", safe=",")
        raw = self._authorized_request(
            f"{GOOGLE_DRIVE_UPLOAD_API}/files?uploadType=multipart&fields={fields}",
            method="POST",
            body=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return self._decode_file_response(raw, "upload")

    def _update_file(
        self,
        file_id: str,
        name: str,
        export_format: WatchlistFormat,
        content: bytes,
    ) -> CloudBackupFile:
        mime_type = "application/json" if export_format == "json" else "application/xml"
        fields = urllib.parse.quote("id,name,modifiedTime,size", safe=",")
        raw = self._authorized_request(
            f"{GOOGLE_DRIVE_UPLOAD_API}/files/{urllib.parse.quote(file_id, safe='')}?uploadType=media&fields={fields}",
            method="PATCH",
            body=content,
            headers={"Content-Type": mime_type},
        )
        result = self._decode_file_response(raw, "update")
        return result if result.name else CloudBackupFile(result.file_id, name, result.modified_time, result.size)

    def _decode_file_response(self, raw: bytes, action: str) -> CloudBackupFile:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudBackupError(f"Google Drive returned an invalid {action} response: {exc}") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise CloudBackupError(f"Google Drive returned an invalid {action} response.")
        return self._file_from_payload(payload)

    def download_backup(self, export_format: WatchlistFormat) -> str:
        with _CLOUD_TRANSFER_LOCK:
            name = AUTO_BACKUP_FILENAMES[export_format]
            remote = self._latest_file(name)
            if remote is None:
                raise CloudBackupError(f"No {name} backup exists in the connected Google Drive account.")
            params = urllib.parse.urlencode({"alt": "media"})
            raw = self._authorized_request(
                f"{GOOGLE_DRIVE_API}/files/{urllib.parse.quote(remote.file_id, safe='')}?{params}"
            )
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CloudBackupError(f"The cloud {export_format.upper()} backup is not valid UTF-8: {exc}") from exc
