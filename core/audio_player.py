"""Lecteur audio par streaming pour l'éditeur de segments (phase 2/3).

Aucune lecture audio n'existait dans l'app ; ce module en ajoute une, taillée pour
la **navigation à l'oreille** d'un fichier potentiellement très long (film 2 h).

Architecture : **un seul thread moteur** possède **un unique flux PortAudio**
(``sounddevice.RawOutputStream``), ouvert une seule fois et réutilisé pour toute la
session d'édition. Les commandes ``play`` / ``scrub`` / ``stop`` ne font que poser
une requête et incrémenter une **génération** ; le moteur abandonne la requête
courante (il tue le ffmpeg en cours) et enchaîne sur la nouvelle **sans jamais
rouvrir le flux**.

Pourquoi ce design : ouvrir/fermer un flux PortAudio à chaque play/scrub — surtout
en rafale (scrub, seek répété) — **crashe en natif** (segfault). Un flux unique,
manipulé par **un seul thread**, supprime à la fois ce crash et tout accès
concurrent (PortAudio n'est pas thread-safe). Bonus : le **seek pendant la lecture**
devient fluide (on relance juste ffmpeg à la nouvelle position, le flux continue).

Décodage : FFmpeg embarqué (``-ss`` puis PCM ``s16le`` stéréo 44,1 kHz sur stdout)
— jamais tout le fichier en RAM, tous les formats de l'app lisibles. API hôte MME
par défaut → **pas de COM** (cf. la leçon accessible_output2 sous Python 3.14).

wx-agnostique : ``on_position`` / ``on_finished`` sont appelés depuis le thread
moteur ; l'appelant les marshale vers l'UI (``wx.CallAfter``).
"""

import atexit
import logging
import subprocess
import threading
import time

from core.ffmpeg_helpers import get_ffmpeg_path


SAMPLE_RATE = 44100
CHANNELS = 2
BYTES_PER_SAMPLE = 2  # s16le
BYTES_PER_MS = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE / 1000.0
_READ_CHUNK = 8192  # octets lus par itération (~46 ms stéréo 16 bits)
_POSITION_INTERVAL_MS = 100  # cadence de remontée du playhead (~10 Hz)
SCRUB_WINDOW_MS = 200  # durée d'un aperçu de scrub


def _format_ss(ms):
    return f"{max(0, ms) / 1000.0:.3f}"


class AudioPlayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._request = None       # dict de la requête courante (ou None = silence)
        self._generation = 0       # incrémenté à chaque play/scrub/stop/shutdown
        self._playing = False
        self._shutdown = False
        self._thread = None
        self.ffmpeg_exe = get_ffmpeg_path()

    # ------------------------------------------------------------------ API
    def is_playing(self):
        with self._lock:
            return self._playing

    def play(self, path, start_ms=0, end_ms=None, on_position=None, on_finished=None,
             audio_index=None):
        """Joue depuis ``start_ms`` (jusqu'à ``end_ms`` si fourni). ``audio_index``
        sélectionne la Nᵉ piste audio (``-map 0:a:N``) pour l'aperçu, None = piste
        par défaut. Remplace toute lecture en cours de façon fluide."""
        with self._lock:
            self._generation += 1
            self._request = {
                'gen': self._generation, 'path': path, 'start': int(start_ms),
                'end': end_ms, 'on_position': on_position, 'on_finished': on_finished,
                'audio_index': audio_index,
            }
            self._playing = True
            self._cv.notify()
        self._ensure_thread()

    def scrub(self, path, pos_ms, window_ms=SCRUB_WINDOW_MS, audio_index=None):
        """Rejoue une courte fenêtre à ``pos_ms`` (aperçu façon scrub), en coupant
        l'aperçu précédent."""
        self.play(path, int(pos_ms), int(pos_ms) + int(window_ms), audio_index=audio_index)

    def stop(self):
        """Arrête la lecture (non bloquant). Le thread moteur reste vivant pour la
        prochaine commande — c'est ``shutdown()`` qui le termine."""
        with self._lock:
            self._generation += 1
            self._request = None
            self._playing = False
            self._cv.notify()

    def shutdown(self):
        """Termine le thread moteur et ferme le flux. À appeler à la fermeture de
        l'éditeur pour ne laisser aucun flux PortAudio actif."""
        with self._lock:
            self._shutdown = True
            self._generation += 1
            self._request = None
            self._playing = False
            self._cv.notify()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        try:
            atexit.unregister(self.shutdown)
        except Exception:
            pass

    # ------------------------------------------------------------------ interne
    def _ensure_thread(self):
        with self._lock:
            if self._thread is None and not self._shutdown:
                self._thread = threading.Thread(target=self._engine, daemon=True, name='audio-engine')
                self._thread.start()

    def _engine(self):
        import sounddevice as sd  # import tardif : ne charge PortAudio qu'à la 1re lecture
        # Le flux est démarré UNE SEULE fois et n'est jamais stoppé/aborté en cours
        # de session (seulement fermé à shutdown) : les cycles start/stop/abort
        # répétés déstabilisent PortAudio (crash natif). Quand rien ne joue, le flux
        # reste actif et « sous-alimenté » → il sort simplement du silence, sans
        # danger. Un seul thread touche le flux (PortAudio n'est pas thread-safe).
        try:
            stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='int16', latency='low')
            stream.start()
        except Exception:
            logging.exception("AudioPlayer : ouverture du flux audio impossible.")
            with self._lock:
                self._playing = False
            return

        # Filet à la sortie de l'interpréteur : fermer le flux AVANT que sounddevice
        # ne termine PortAudio (sinon segfault natif si un flux est encore actif).
        # atexit LIFO → notre shutdown passe avant le _terminate de sounddevice.
        atexit.register(self.shutdown)

        try:
            while True:
                with self._lock:
                    while self._request is None and not self._shutdown:
                        self._cv.wait()
                    if self._shutdown:
                        break
                    req = self._request
                    gen = req['gen']

                self._run_request(stream, req, gen)

                # Requête terminée (fin naturelle / remplacée / stoppée) : si aucune
                # requête plus récente n'attend, on repasse au silence — le flux
                # reste actif, on cesse juste de lui écrire des données.
                with self._lock:
                    if self._generation == gen:
                        self._request = None
                        self._playing = False
        finally:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

    def _run_request(self, stream, req, gen):
        cmd = [self.ffmpeg_exe, '-hide_banner', '-loglevel', 'quiet',
               '-ss', _format_ss(req['start']), '-i', req['path']]
        if req.get('audio_index') is not None:
            cmd.extend(['-map', f"0:a:{int(req['audio_index'])}"])
        if req['end'] is not None and req['end'] > req['start']:
            cmd.extend(['-t', _format_ss(req['end'] - req['start'])])
        cmd.extend(['-vn', '-f', 's16le', '-acodec', 'pcm_s16le',
                    '-ac', str(CHANNELS), '-ar', str(SAMPLE_RATE), '-'])

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        process = None
        completed = False
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            on_position = req['on_position']
            start_ms = req['start']
            bytes_written = 0
            last_report_ms = -_POSITION_INTERVAL_MS
            while True:
                with self._lock:
                    if gen != self._generation:
                        break  # remplacée / stoppée → on rend la main au moteur
                data = process.stdout.read(_READ_CHUNK)
                if not data:
                    with self._lock:
                        completed = gen == self._generation  # vrai EOF, pas un kill
                    break
                stream.write(data)  # bloque → cale la lecture sur le temps réel
                bytes_written += len(data)
                if on_position is not None:
                    played_ms = bytes_written / BYTES_PER_MS
                    if played_ms - last_report_ms >= _POSITION_INTERVAL_MS:
                        last_report_ms = played_ms
                        on_position(int(start_ms + played_ms))

            if completed:
                # Laisse le tampon se vider avant la fin, puis position finale.
                try:
                    time.sleep(float(getattr(stream, 'latency', 0.0)) + 0.05)
                except Exception:
                    pass
                if on_position is not None:
                    on_position(int(start_ms + bytes_written / BYTES_PER_MS))
        except Exception:
            logging.exception("AudioPlayer : erreur pendant la lecture.")
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass

        if completed and req['on_finished'] is not None:
            with self._lock:
                still_current = gen == self._generation
            if still_current:
                req['on_finished']()
