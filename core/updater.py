import builtins
import hashlib
import json
import os
import re
import subprocess
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib import error, request

from core.app_info import (
    APP_EXE_NAME,
    APP_GITHUB_RELEASES_API,
    APP_GITHUB_RELEASES_PAGE,
    APP_INSTALLER_FILENAME,
    APP_VERSION,
)

UPDATES_DIRNAME = "updates"
UPDATER_STATE_FILENAME = "updater-state.json"
HTTP_TIMEOUT_SECONDS = 10
DOWNLOAD_CHUNK_SIZE = 1024 * 256

# Must match the markers emitted by scripts/build_release.ps1 (AME-RELEASE-NOTES).
_NOTES_MARKER_RE = re.compile(
    r'<!--\s*AME-RELEASE-NOTES:([a-z]{2}):start\s*-->(.+?)<!--\s*AME-RELEASE-NOTES:\1:end\s*-->',
    re.DOTALL,
)


class UpdaterError(Exception):
    pass


class UpdateCheckError(UpdaterError):
    pass


class UpdateDownloadError(UpdaterError):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    tag_name: str
    version: str
    published_at: str
    html_url: str
    body: str
    asset_name: str
    asset_url: str
    asset_digest: str = ""


def _translate(msgid):
    translator = builtins.__dict__.get("_")
    if callable(translator):
        return translator(msgid)
    return msgid


def get_local_appdata_dir():
    localappdata = os.getenv("LOCALAPPDATA")
    if localappdata:
        return Path(localappdata)
    return Path.home() / "AppData" / "Local"


def get_update_root_dir():
    return get_local_appdata_dir() / APP_EXE_NAME


def get_updates_dir():
    return get_update_root_dir() / UPDATES_DIRNAME


def ensure_updates_dir():
    updates_dir = get_updates_dir()
    updates_dir.mkdir(parents=True, exist_ok=True)
    return updates_dir


def get_updater_state_path():
    return get_update_root_dir() / UPDATER_STATE_FILENAME


def normalize_version(value):
    normalized = str(value or "").strip()
    if normalized.lower().startswith("v"):
        normalized = normalized[1:]
    return normalized


def parse_version_tuple(value):
    normalized = normalize_version(value)
    if not normalized:
        return tuple()

    parts = []
    for token in normalized.split("."):
        digits = []
        for char in token:
            if char.isdigit():
                digits.append(char)
            else:
                break
        if not digits:
            parts.append(0)
            continue
        parts.append(int("".join(digits)))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def parse_version_key(value):
    """Total order over version strings, prerelease-aware (SemVer-style).

    A prerelease (``0.2.0-rc2``) sorts BELOW its final release (``0.2.0``), so
    a stable build published after a prerelease is correctly seen as newer. This
    is what lets a user running ``0.2.0-rc2`` be offered the ``0.2.0`` stable —
    provided the prerelease build carries the ``-rcN`` suffix in ``APP_VERSION``.
    """
    normalized = normalize_version(value)
    release_part, separator, prerelease_part = normalized.partition("-")
    release_tuple = parse_version_tuple(release_part)
    if not separator or not prerelease_part:
        # Final release: higher precedence than any prerelease of the same number.
        return (release_tuple, (1,), ())

    identifiers = []
    for token in re.findall(r"\d+|[A-Za-z]+", prerelease_part):
        if token.isdigit():
            # Numeric identifiers rank below alphabetic ones (SemVer §11), and
            # compare numerically: rc10 > rc2.
            identifiers.append((0, int(token), ""))
        else:
            identifiers.append((1, 0, token.lower()))
    return (release_tuple, (0,), tuple(identifiers))


def is_release_newer(remote_version, current_version=APP_VERSION):
    return parse_version_key(remote_version) > parse_version_key(current_version)


def fetch_latest_release(timeout=HTTP_TIMEOUT_SECONDS, lang=None, include_prereleases=False):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{APP_EXE_NAME}/{APP_VERSION}",
    }
    api_request = request.Request(APP_GITHUB_RELEASES_API, headers=headers)

    try:
        with request.urlopen(api_request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise UpdateCheckError(
            _translate("GitHub update check failed with HTTP status {code}.").format(code=exc.code)
        ) from exc
    except error.URLError as exc:
        raise UpdateCheckError(_translate("Unable to contact GitHub to check for updates.")) from exc
    except TimeoutError as exc:
        raise UpdateCheckError(_translate("The update check timed out.")) from exc
    except json.JSONDecodeError as exc:
        raise UpdateCheckError(_translate("GitHub returned an invalid update response.")) from exc

    return parse_release_info(payload, lang=lang, include_prereleases=include_prereleases)


def parse_release_info(payload, lang=None, include_prereleases=False):
    candidate_releases = _extract_candidate_releases(payload, include_prereleases)
    if not candidate_releases:
        raise UpdateCheckError(_translate("GitHub returned an invalid update response."))

    # Pick the highest version among the candidates rather than trusting GitHub's
    # publish order, so a stable can outrank an earlier-listed prerelease (and
    # vice-versa).
    candidate_releases.sort(key=lambda item: parse_version_key(item.get("tag_name")), reverse=True)

    latest_release = candidate_releases[0]
    tag_name = str(latest_release.get("tag_name") or "").strip()
    version = normalize_version(tag_name)
    html_url = str(latest_release.get("html_url") or APP_GITHUB_RELEASES_PAGE).strip() or APP_GITHUB_RELEASES_PAGE
    body = build_combined_release_notes(candidate_releases, lang=lang)
    published_at = str(latest_release.get("published_at") or "").strip()
    asset_name, asset_url, asset_digest = find_setup_asset(latest_release.get("assets"))

    if not version:
        raise UpdateCheckError(_translate("The GitHub release does not define a valid version tag."))

    return ReleaseInfo(
        tag_name=tag_name,
        version=version,
        published_at=published_at,
        html_url=html_url,
        body=body,
        asset_name=asset_name,
        asset_url=asset_url,
        asset_digest=asset_digest,
    )


def _extract_candidate_releases(payload, include_prereleases=False):
    if not isinstance(payload, list):
        return []

    candidates = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("draft"):
            continue
        if item.get("prerelease") and not include_prereleases:
            continue
        candidates.append(item)
    return candidates


def build_combined_release_notes(releases, current_version=APP_VERSION, lang=None):
    sections = []
    for release in releases:
        version = normalize_version(release.get("tag_name"))
        if not version or not is_release_newer(version, current_version):
            continue

        published_at = format_release_date(str(release.get("published_at") or "").strip())
        body = normalize_release_notes(release.get("body"), lang=lang)
        sections.append(
            "\n".join(
                [
                    f"# {_translate('Version')} {version}",
                    f"{_translate('Published:')} {published_at}",
                    "",
                    body,
                ]
            ).strip()
        )

    if sections:
        return "\n\n".join(sections)

    latest_release = releases[0] if releases else {}
    return normalize_release_notes(latest_release.get("body"), lang=lang)


def extract_language_notes(body, lang):
    """Extract notes for a specific language from a bilingual release body.

    Falls back: requested lang -> 'en' -> 'fr' -> full body if no markers found.
    """
    normalized = str(body or "").replace("\r\n", "\n")
    matches = {m.group(1): m.group(2).strip() for m in _NOTES_MARKER_RE.finditer(normalized)}

    if not matches:
        return normalized.strip()

    for candidate in [lang, "en", "fr"]:
        if candidate and candidate in matches:
            return matches[candidate]

    return normalized.strip()


def normalize_release_notes(value, lang=None):
    normalized = str(value or "").replace("\r\n", "\n").strip()
    if not normalized:
        return _translate("No release notes provided.")
    if lang:
        normalized = extract_language_notes(normalized, lang)
    return normalized or _translate("No release notes provided.")


def format_release_date(value):
    if not value:
        return _translate("Unknown")

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def find_setup_asset(assets):
    if not isinstance(assets, list):
        raise UpdateCheckError(_translate("No installer asset was found in the GitHub release."))

    exact_name = APP_INSTALLER_FILENAME.lower()
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").strip()
        if name.lower() == exact_name:
            download_url = str(asset.get("browser_download_url") or "").strip()
            if download_url:
                digest = str(asset.get("digest") or "").strip()
                return name, download_url, digest

    raise UpdateCheckError(_translate("No installer asset was found in the GitHub release."))


def parse_expected_sha256(digest):
    """Return the lowercase hex SHA-256 from a GitHub asset ``digest`` field.

    GitHub formats it as ``sha256:<hex>``. Returns None when the field is
    absent or uses an algorithm we do not verify (forward-compatible: we never
    fail an update just because GitHub introduced a new digest format).
    """
    normalized = str(digest or "").strip().lower()
    if not normalized.startswith("sha256:"):
        return None
    hex_digest = normalized[len("sha256:"):].strip()
    if len(hex_digest) != 64 or any(char not in "0123456789abcdef" for char in hex_digest):
        return None
    return hex_digest


def download_release_installer(release_info, progress_callback=None, timeout=HTTP_TIMEOUT_SECONDS):
    if not isinstance(release_info, ReleaseInfo):
        raise UpdateDownloadError(_translate("Invalid update information."))

    updates_dir = ensure_updates_dir()
    final_path = updates_dir / release_info.asset_name
    partial_path = updates_dir / f"{release_info.asset_name}.part"

    if partial_path.exists():
        partial_path.unlink(missing_ok=True)

    headers = {"User-Agent": f"{APP_EXE_NAME}/{APP_VERSION}"}
    asset_request = request.Request(release_info.asset_url, headers=headers)

    expected_sha256 = parse_expected_sha256(release_info.asset_digest)

    try:
        with request.urlopen(asset_request, timeout=timeout) as response:
            total_size = int(response.headers.get("Content-Length", "0") or "0")
            downloaded = 0
            hasher = hashlib.sha256()
            with open(partial_path, "wb") as handle:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    if progress_callback is not None:
                        progress_callback(downloaded, total_size)

        # Refuse a truncated download (connection dropped without raising).
        if total_size and downloaded != total_size:
            raise UpdateDownloadError(_translate("The installer download was incomplete. Please try again."))

        # Verify integrity against the SHA-256 GitHub publishes for the asset.
        if expected_sha256 and hasher.hexdigest() != expected_sha256:
            raise UpdateDownloadError(
                _translate("The installer failed its integrity check and was not installed.")
            )

        if final_path.exists():
            final_path.unlink()
        partial_path.replace(final_path)
        return final_path
    except error.HTTPError as exc:
        raise UpdateDownloadError(
            _translate("The installer download failed with HTTP status {code}.").format(code=exc.code)
        ) from exc
    except error.URLError as exc:
        raise UpdateDownloadError(_translate("Unable to download the installer from GitHub.")) from exc
    except TimeoutError as exc:
        raise UpdateDownloadError(_translate("The installer download timed out.")) from exc
    except OSError as exc:
        raise UpdateDownloadError(_translate("Unable to save the downloaded installer.")) from exc
    finally:
        if partial_path.exists():
            partial_path.unlink(missing_ok=True)


def load_updater_state():
    state_path = get_updater_state_path()
    if not state_path.exists():
        return {}

    try:
        with open(state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        return {}
    return {}


def save_updater_state(installer_path, version, cleanup_pending=True):
    state_path = get_updater_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "downloaded_installer_path": str(Path(installer_path)),
        "downloaded_version": normalize_version(version),
        "cleanup_pending": bool(cleanup_pending),
    }
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=4)
    return payload


def clear_updater_state():
    state_path = get_updater_state_path()
    if state_path.exists():
        state_path.unlink(missing_ok=True)


def cleanup_update_artifacts():
    updates_dir = get_updates_dir()
    state = load_updater_state()
    removed_paths = []
    keep_pending = False
    pending_installer = str(state.get("downloaded_installer_path") or "").strip()
    cleanup_pending = bool(state.get("cleanup_pending", False))

    if cleanup_pending and pending_installer:
        pending_path = Path(pending_installer)
        if pending_path.exists():
            try:
                pending_path.unlink()
                removed_paths.append(str(pending_path))
            except OSError:
                keep_pending = True
        else:
            removed_paths.append(str(pending_path))

    if updates_dir.exists():
        normalized_pending = os.path.normcase(pending_installer) if pending_installer else ""
        stale_candidates = []
        exact_installer_path = updates_dir / APP_INSTALLER_FILENAME
        if exact_installer_path.exists():
            stale_candidates.append(exact_installer_path)

        for stale_path in stale_candidates:
            if normalized_pending and os.path.normcase(str(stale_path)) == normalized_pending:
                continue
            try:
                stale_path.unlink()
                removed_paths.append(str(stale_path))
            except OSError:
                continue

        for partial_path in updates_dir.glob("*.part"):
            try:
                partial_path.unlink()
                removed_paths.append(str(partial_path))
            except OSError:
                continue

    if keep_pending:
        save_updater_state(pending_installer, state.get("downloaded_version", ""), cleanup_pending=True)
    else:
        clear_updater_state()

    return removed_paths


def open_release_page(url):
    target = str(url or APP_GITHUB_RELEASES_PAGE).strip() or APP_GITHUB_RELEASES_PAGE
    if os.name == "nt":
        try:
            os.startfile(target)
            return target
        except OSError:
            pass

    if webbrowser.open(target, new=0):
        return target

    raise RuntimeError("Unable to open the release page.")


def launch_installer_after_exit(installer_path):
    resolved_path = str(Path(installer_path).resolve())
    if os.name != "nt":
        subprocess.Popen([resolved_path], close_fds=True)
        return resolved_path

    escaped_path = resolved_path.replace("'", "''")
    # Mode silencieux Inno : barre de progression seule, sans assistant à cliquer.
    command = (
        f"Start-Sleep -Milliseconds 800; "
        f"Start-Process -FilePath '{escaped_path}' "
        f"-ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART'"
    )
    kwargs = {"close_fds": True}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-Command",
            command,
        ],
        **kwargs,
    )
    return resolved_path
