import builtins
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from core.conversion import (
    ConversionTask,
    build_cue_track_output_path,
    build_output_path,
    build_segment_output_path,
)
from core.segments import kept_regions


def _translate(msgid):
    translator = builtins.__dict__.get('_')
    if callable(translator):
        return translator(msgid)
    return msgid


JOB_STATE_QUEUED = "queued"
JOB_STATE_RUNNING = "running"
JOB_STATE_DONE = "done"
JOB_STATE_SKIPPED = "skipped"
JOB_STATE_ERROR = "error"
JOB_STATE_STOPPED = "stopped"

SKIP_REASON_EXISTS = "exists"
SKIP_REASON_BATCH_STOPPED = "batch_stopped"


@dataclass
class BatchJob:
    index: int
    meta: object
    target_format: str
    settings: dict
    output_path: str | None
    weight: float
    state: str = JOB_STATE_QUEUED
    progress: int = 0
    # Découpage cue : entrée réelle (image audio), tranche temporelle et tags par piste.
    input_path: str | None = None
    clip: tuple | None = None
    tag_overrides: dict | None = None
    skip_reason: str | None = None
    error_message: str = ""
    error_kind: str = ""
    ffmpeg_command: list = field(default_factory=list)
    ffmpeg_stderr: str = ""


class BatchConversionManager:
    def __init__(
        self,
        media_list,
        target_format,
        settings,
        output_dir=None,
        max_concurrent=2,
        output_policy="rename",
        continue_on_error=True,
        preserve_structure=False,
        on_job_update=None,
        on_batch_update=None,
        on_batch_complete=None,
    ):
        self.media_list = list(media_list)
        self.target_format = target_format
        self.settings = dict(settings)
        self.output_dir = output_dir
        self.max_concurrent = max(1, int(max_concurrent))
        self.output_policy = output_policy
        self.continue_on_error = bool(continue_on_error)
        self.preserve_structure = bool(preserve_structure)
        self.on_job_update = on_job_update
        self.on_batch_update = on_batch_update
        self.on_batch_complete = on_batch_complete

        self._state_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._active_tasks = {}
        self._controller_thread = None
        self._abort_new_jobs = False

        self._start_time = None

        self.jobs = self._prepare_jobs()
        self.primary_output_dir = self._resolve_primary_output_dir()

    def start(self):
        if self._controller_thread and self._controller_thread.is_alive():
            return self._controller_thread
        self._controller_thread = threading.Thread(target=self._run, daemon=True, name="batch-conversion-manager")
        self._controller_thread.start()
        return self._controller_thread

    def stop(self):
        self._stop_requested.set()
        with self._state_lock:
            active_tasks = list(self._active_tasks.values())

        for task in active_tasks:
            task.stop()

    def _prepare_jobs(self):
        reserved_paths = set()
        jobs = []
        for meta in self.media_list:
            if getattr(meta, 'segment_plan', None) is not None:
                self._append_segment_jobs(jobs, meta, reserved_paths)
            elif getattr(meta, 'cue_sheet', None) is not None:
                self._append_cue_jobs(jobs, meta, reserved_paths)
            else:
                self._append_normal_job(jobs, meta, reserved_paths)
        return jobs

    def _append_segment_jobs(self, jobs, meta, reserved_paths):
        """Développe un plan de découpage manuel en un job par région gardée
        (-ss/-t sur le fichier source). Utilisé pour l'export « N fichiers » ; le
        mode « 1 fichier reconcaténé » passe lui par SegmentExportTask, hors batch."""
        target_format, job_settings = self._resolve_job_format_settings(meta)
        regions = kept_regions(meta.segment_plan)

        if not regions:
            jobs.append(BatchJob(
                index=len(jobs), meta=meta, target_format=target_format, settings=job_settings,
                output_path=None, weight=1.0, state=JOB_STATE_ERROR,
                error_message=_translate("At least one segment must be kept."),
            ))
            return

        total = len(regions)
        for position, (start_ms, end_ms) in enumerate(regions, start=1):
            segment = self._segment_for_region(meta, start_ms, end_ms)
            label = segment.label if segment is not None else ""
            base_output_path = build_segment_output_path(
                meta.full_path, position, total, label, target_format,
                custom_output_dir=self.output_dir,
                relative_dir=self._meta_relative_dir(meta),
            )
            resolved_output_path, skip_reason = self._reserve_output_path(
                base_output_path, reserved_paths, meta.full_path,
            )
            weight = max((end_ms - start_ms) / 1000.0, 0.1)
            job = BatchJob(
                index=len(jobs),
                meta=meta,
                target_format=target_format,
                settings=job_settings,
                output_path=resolved_output_path,
                weight=weight,
                input_path=meta.full_path,
                clip=(start_ms, end_ms),
                tag_overrides={'title': label} if label else None,
            )
            if skip_reason:
                job.state = JOB_STATE_SKIPPED
                job.progress = 100
                job.skip_reason = skip_reason
            jobs.append(job)

    @staticmethod
    def _segment_for_region(meta, start_ms, end_ms):
        """Retrouve le segment source d'une région gardée (pour récupérer son
        label). Les régions gardées adjacentes ayant été fusionnées, on identifie
        par le début."""
        for seg in getattr(meta.segment_plan, 'segments', []):
            if seg.keep and seg.start_ms == start_ms:
                return seg
        return None

    def _append_normal_job(self, jobs, meta, reserved_paths):
        target_format, job_settings = self._resolve_job_format_settings(meta)
        base_output_path = build_output_path(
            meta.full_path, target_format, custom_output_dir=self.output_dir,
            relative_dir=self._meta_relative_dir(meta),
        )
        resolved_output_path, skip_reason = self._reserve_output_path(
            base_output_path,
            reserved_paths,
            meta.full_path,
        )
        job = BatchJob(
            index=len(jobs),
            meta=meta,
            target_format=target_format,
            settings=job_settings,
            output_path=resolved_output_path,
            weight=self._get_job_weight(meta),
        )
        if skip_reason:
            job.state = JOB_STATE_SKIPPED
            job.progress = 100
            job.skip_reason = skip_reason
        jobs.append(job)

    def _append_cue_jobs(self, jobs, meta, reserved_paths):
        """Développe une image album (cue) en un job par piste (-ss/-t + tags)."""
        target_format, job_settings = self._resolve_job_format_settings(meta)
        sheet = meta.cue_sheet
        audio_path = getattr(sheet, 'audio_ref', None)
        error = getattr(meta, 'cue_error', None)

        if error or not audio_path or not sheet.tracks:
            # Ligne cue invalide : un job en erreur (output_path None → non exécuté).
            jobs.append(BatchJob(
                index=len(jobs), meta=meta, target_format=target_format, settings=job_settings,
                output_path=None, weight=1.0, state=JOB_STATE_ERROR,
                error_message=error or _translate("This cue sheet cannot be split."),
            ))
            return

        total = len(sheet.tracks)
        for track in sheet.tracks:
            base_output_path = build_cue_track_output_path(
                audio_path, sheet.album, track.number, total, track.title,
                target_format, custom_output_dir=self.output_dir,
                relative_dir=self._meta_relative_dir(meta),
            )
            resolved_output_path, skip_reason = self._reserve_output_path(
                base_output_path, reserved_paths, audio_path,
            )
            weight = (track.end_ms - track.start_ms) / 1000.0 if track.end_ms else 1.0
            job = BatchJob(
                index=len(jobs),
                meta=meta,
                target_format=target_format,
                settings=job_settings,
                output_path=resolved_output_path,
                weight=max(weight, 0.1),
                input_path=audio_path,
                clip=(track.start_ms, track.end_ms),
                tag_overrides=self._build_cue_tags(sheet, track, total),
            )
            if skip_reason:
                job.state = JOB_STATE_SKIPPED
                job.progress = 100
                job.skip_reason = skip_reason
            jobs.append(job)

    @staticmethod
    def _build_cue_tags(sheet, track, total):
        artist = track.performer or sheet.album_performer
        tags = {'track': f"{track.number}/{total}"}
        if track.title:
            tags['title'] = track.title
        if artist:
            tags['artist'] = artist
        if sheet.album:
            tags['album'] = sheet.album
        if sheet.album_performer:
            tags['album_artist'] = sheet.album_performer
        if sheet.date:
            tags['date'] = sheet.date
        if sheet.genre:
            tags['genre'] = sheet.genre
        return tags

    def _meta_relative_dir(self, meta):
        """Sous-dossier relatif à recréer sous la sortie, seulement si la
        préférence est active (sinon sortie à plat, comportement historique)."""
        if not self.preserve_structure:
            return ""
        return getattr(meta, 'relative_dir', '') or ""

    def _resolve_job_format_settings(self, meta):
        """Retourne (format, settings) pour ce fichier : son override de sortie
        s'il en a un, sinon le format/réglages globaux du batch. Les réglages non
        liés au format (threads, préservation des métadonnées) sont hérités du global."""
        override = getattr(meta, 'output_override', None)
        if isinstance(override, dict) and override.get('format'):
            job_settings = dict(override.get('settings') or {})
            for global_key in ('ffmpeg_threads', 'preserve_metadata'):
                if global_key in self.settings:
                    job_settings.setdefault(global_key, self.settings[global_key])
            return override['format'], job_settings
        return self.target_format, dict(self.settings)

    def _reserve_output_path(self, base_output_path, reserved_paths, input_path):
        candidate_path = base_output_path
        suffix = 1
        input_key = self._path_key(input_path)

        while True:
            candidate_key = self._path_key(candidate_path)
            collides_with_input = candidate_key == input_key
            reserved_collision = candidate_key in reserved_paths
            file_exists = os.path.exists(candidate_path)

            if not reserved_collision and not collides_with_input:
                if self.output_policy == "overwrite":
                    reserved_paths.add(candidate_key)
                    return candidate_path, None
                if not file_exists:
                    reserved_paths.add(candidate_key)
                    return candidate_path, None

            # On ne « skippe » jamais à cause du fichier source lui-même :
            # dans ce cas on force un renommage pour produire une copie sûre.
            if self.output_policy == "skip" and not collides_with_input:
                return None, SKIP_REASON_EXISTS

            candidate_path = self._append_suffix(base_output_path, suffix)
            suffix += 1

    def _resolve_primary_output_dir(self):
        if self.output_dir:
            return self.output_dir
        for job in self.jobs:
            if job.output_path:
                return os.path.dirname(job.output_path)
        return None

    def _run(self):
        self._start_time = time.monotonic()
        runnable_jobs = [job for job in self.jobs if job.output_path]
        for job in self.jobs:
            self._emit_job_update(job)
        self._emit_batch_update()

        pending_jobs = list(runnable_jobs)
        active_futures = {}

        with ThreadPoolExecutor(max_workers=self.max_concurrent, thread_name_prefix="conversion-job") as executor:
            while pending_jobs or active_futures:
                while (
                    pending_jobs
                    and len(active_futures) < self.max_concurrent
                    and not self._stop_requested.is_set()
                    and not self._abort_new_jobs
                ):
                    job = pending_jobs.pop(0)
                    future = executor.submit(self._run_job, job)
                    active_futures[future] = job

                if not active_futures:
                    break

                done, _ = wait(list(active_futures.keys()), timeout=0.1, return_when=FIRST_COMPLETED)
                if not done:
                    continue

                for future in done:
                    job = active_futures.pop(future)
                    try:
                        result_state = future.result()
                    except Exception as exc:
                        job.error_message = str(exc)
                        self._set_job_state(job, JOB_STATE_ERROR, error_message=str(exc))
                        result_state = JOB_STATE_ERROR

                    if result_state == JOB_STATE_ERROR and not self.continue_on_error:
                        self._abort_new_jobs = True

            final_pending_reason = None
            final_pending_state = None
            if self._stop_requested.is_set():
                final_pending_state = JOB_STATE_STOPPED
            elif self._abort_new_jobs:
                final_pending_state = JOB_STATE_SKIPPED
                final_pending_reason = SKIP_REASON_BATCH_STOPPED

            while pending_jobs:
                job = pending_jobs.pop(0)
                self._set_job_state(job, final_pending_state, skip_reason=final_pending_reason)

        self._emit_batch_complete()

    def _run_job(self, job):
        task = ConversionTask(
            job.meta,
            job.target_format,
            job.settings,
            output_dir=self.output_dir,
            output_path=job.output_path,
            clip=job.clip,
            extra_tags=job.tag_overrides,
            input_path_override=job.input_path,
        )
        with self._state_lock:
            self._active_tasks[job.index] = task

        self._set_job_state(job, JOB_STATE_RUNNING, progress=0)
        try:
            task.run(
                progress_callback=lambda pct: self._update_job_progress(job.index, pct),
                stop_check_callback=self._stop_requested.is_set,
            )
        except Exception as exc:
            job.ffmpeg_command = list(task.last_command) if task.last_command else []
            job.ffmpeg_stderr = "\n".join(task.stderr_lines[-50:]) if task.stderr_lines else ""

            if str(exc) == "Stopped by user" or self._stop_requested.is_set():
                self._set_job_state(job, JOB_STATE_STOPPED)
                return JOB_STATE_STOPPED

            if isinstance(exc, FileNotFoundError):
                job.error_kind = "input_missing"
            self._set_job_state(job, JOB_STATE_ERROR, error_message=str(exc))
            return JOB_STATE_ERROR
        finally:
            with self._state_lock:
                self._active_tasks.pop(job.index, None)

        self._set_job_state(job, JOB_STATE_DONE, progress=100)
        return JOB_STATE_DONE

    def _update_job_progress(self, job_index, progress):
        progress = max(0, min(int(progress), 100))
        with self._state_lock:
            job = self.jobs[job_index]
            if job.state != JOB_STATE_RUNNING:
                return
            job.progress = progress
            summary = self._build_summary()
            event = self._build_job_event(job)
            event['eta_seconds'] = summary.get('eta_seconds')

        self._dispatch_job_update(event)
        self._dispatch_batch_update(summary)

    def _set_job_state(self, job, state, progress=None, skip_reason=None, error_message=None):
        if state is None:
            return

        with self._state_lock:
            job.state = state
            if progress is not None:
                job.progress = max(0, min(int(progress), 100))
            elif state in (JOB_STATE_DONE, JOB_STATE_SKIPPED, JOB_STATE_ERROR):
                job.progress = 100
            job.skip_reason = skip_reason
            if error_message:
                job.error_message = error_message
            event = self._build_job_event(job)
            summary = self._build_summary()

        self._dispatch_job_update(event)
        self._dispatch_batch_update(summary)

    def _emit_job_update(self, job):
        with self._state_lock:
            event = self._build_job_event(job)
        self._dispatch_job_update(event)

    def _emit_batch_update(self):
        with self._state_lock:
            summary = self._build_summary()
        self._dispatch_batch_update(summary)

    def _emit_batch_complete(self):
        with self._state_lock:
            summary = self._build_summary()
        summary["user_stopped"] = self._stop_requested.is_set()
        summary["aborted_after_error"] = self._abort_new_jobs and not self._stop_requested.is_set()
        if self.on_batch_complete:
            self.on_batch_complete(summary)

    def _dispatch_job_update(self, event):
        if self.on_job_update:
            self.on_job_update(event)

    def _dispatch_batch_update(self, summary):
        if self.on_batch_update:
            self.on_batch_update(summary)

    def _build_job_event(self, job):
        return {
            "index": job.index,
            "state": job.state,
            "progress": job.progress,
            "skip_reason": job.skip_reason,
            "error_message": job.error_message,
            "error_kind": job.error_kind,
            "output_path": job.output_path,
            "input_path": getattr(job.meta, 'full_path', '') if job.meta else '',
            "target_format": job.target_format,
            "ffmpeg_command": job.ffmpeg_command,
            "ffmpeg_stderr": job.ffmpeg_stderr,
            "settings": job.settings,
        }

    def _build_summary(self):
        counts = {
            JOB_STATE_QUEUED: 0,
            JOB_STATE_RUNNING: 0,
            JOB_STATE_DONE: 0,
            JOB_STATE_SKIPPED: 0,
            JOB_STATE_ERROR: 0,
            JOB_STATE_STOPPED: 0,
        }
        total_weight = 0.0
        accumulated_progress = 0.0
        stopped_output_paths = []

        for job in self.jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
            weight = job.weight
            total_weight += weight
            accumulated_progress += weight * job.progress
            if job.state == JOB_STATE_STOPPED and job.output_path:
                stopped_output_paths.append(job.output_path)

        overall_progress = 0
        if total_weight > 0:
            overall_progress = int(accumulated_progress / total_weight)

        eta_seconds = None
        if self._start_time is not None and overall_progress >= 5:
            elapsed = time.monotonic() - self._start_time
            if elapsed >= 2.0:
                computed = int(elapsed * (100 - overall_progress) / overall_progress)
                if computed > 0:
                    eta_seconds = computed

        return {
            "total": len(self.jobs),
            "queued": counts[JOB_STATE_QUEUED],
            "running": counts[JOB_STATE_RUNNING],
            "done": counts[JOB_STATE_DONE],
            "skipped": counts[JOB_STATE_SKIPPED],
            "error": counts[JOB_STATE_ERROR],
            "stopped": counts[JOB_STATE_STOPPED],
            "overall_progress": max(0, min(overall_progress, 100)),
            "eta_seconds": eta_seconds,
            "primary_output_dir": self.primary_output_dir,
            "stopped_output_paths": stopped_output_paths,
        }

    @staticmethod
    def _get_job_weight(meta):
        try:
            duration = float(getattr(meta, "duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        return duration if duration > 0 else 1.0

    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path))

    @staticmethod
    def _append_suffix(path, suffix):
        root, ext = os.path.splitext(path)
        return f"{root} ({suffix}){ext}"
