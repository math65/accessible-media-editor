"""Automatic error reporting: verbose FFmpeg re-run and report payload construction."""
import os
import subprocess

from core.support import send_support_report

VERBOSE_RERUN_TIMEOUT = 30


def rerun_ffmpeg_verbose(original_cmd, timeout=VERBOSE_RERUN_TIMEOUT):
    """Re-run the failed FFmpeg command with -loglevel verbose and capture stderr.

    Returns the captured stderr output as a string.
    """
    if not original_cmd:
        return "[No FFmpeg command available for diagnostic re-run]"

    cmd = list(original_cmd)
    cmd.insert(1, '-loglevel')
    cmd.insert(2, 'verbose')

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.PIPE,
            timeout=timeout,
            encoding='utf-8',
            errors='ignore',
            startupinfo=startupinfo,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        captured = ""
        if exc.stderr:
            captured = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode('utf-8', errors='ignore')
        return captured + f"\n[Diagnostic timed out after {timeout} seconds]"
    except Exception as exc:  # noqa: BLE001
        return f"[Diagnostic re-run failed: {exc}]"


def build_error_report_message(input_path, target_format, ffmpeg_stderr, user_comment=""):
    """Build the user-facing message body for the error report."""
    filename = os.path.basename(input_path) if input_path else "unknown"
    lines = [
        "Automatic error report — cut/export failure",
        f"File: {filename}",
        f"Target format: {target_format}",
        "",
        "FFmpeg error output:",
        ffmpeg_stderr or "(no output captured)",
    ]
    if user_comment and user_comment.strip():
        lines.extend(["", "User comment:", user_comment.strip()])
    return "\n".join(lines)


def send_error_report(
    email,
    input_path,
    target_format,
    ffmpeg_stderr,
    verbose_log,
    user_comment,
    support_context,
):
    """Send the error report using the existing support report API.

    Raises SupportSendError on failure.
    """
    message = build_error_report_message(input_path, target_format, ffmpeg_stderr, user_comment)
    send_support_report(
        email_address=email,
        issue_type="cut_export_problem",
        user_message=message,
        context=support_context,
        debug_log=verbose_log,
    )
