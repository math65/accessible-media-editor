"""Modèle de découpage manuel d'un média en segments (garder / jeter).

L'utilisateur pose des points de coupe sur la timeline ; le tronçon entre deux
coupes consécutives est un :class:`Segment`, dont on bascule le drapeau ``keep``.
Un :class:`SegmentPlan` **pave toujours** tout l'intervalle ``[0, duration_ms]``
(segments contigus, triés, sans recouvrement) : c'est l'invariant maintenu par
toutes les fonctions de ce module.

Ce module est **pur** (aucune dépendance wx / ffmpeg) et testable isolément. En
aval, deux modes d'export s'appuient sur :func:`kept_regions`, la source de
vérité :

- **1 fichier reconcaténé** = concat des régions gardées (retirer les pubs) ;
- **N fichiers** = une sortie par région gardée.
"""

import builtins
from dataclasses import dataclass, field


def _translate(msgid):
    translator = builtins.__dict__.get('_')
    if callable(translator):
        return translator(msgid)
    return msgid


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    keep: bool = True
    label: str = ""

    @property
    def duration_ms(self):
        return max(0, self.end_ms - self.start_ms)


@dataclass
class SegmentPlan:
    duration_ms: int
    segments: list = field(default_factory=list)


def new_plan(duration_ms):
    """Plan initial : un seul segment gardé couvrant tout ``[0, duration_ms]``."""
    duration_ms = max(0, int(duration_ms))
    return SegmentPlan(duration_ms=duration_ms, segments=[Segment(0, duration_ms, keep=True)])


def _normalize(plan):
    """Rétablit l'invariant : segments triés, clampés, contigus, sans trou ni
    recouvrement, couvrant exactement ``[0, duration_ms]``. Fusionne les segments
    de longueur nulle avec leur voisin. Idempotent."""
    duration = max(0, int(plan.duration_ms))
    plan.duration_ms = duration

    # Points de coupe = toutes les bornes internes valides, dédupliquées et triées.
    boundaries = {0, duration}
    for seg in plan.segments:
        for value in (seg.start_ms, seg.end_ms):
            if 0 < value < duration:
                boundaries.add(int(value))
    ordered = sorted(boundaries)

    # Le drapeau keep d'un nouveau tronçon = celui du segment source dont le milieu
    # tombe dedans (préserve les choix garder/jeter à travers un re-normalisation).
    rebuilt = []
    for i in range(len(ordered) - 1):
        start, end = ordered[i], ordered[i + 1]
        if end <= start:
            continue
        midpoint = (start + end) / 2
        source = _segment_at(plan.segments, midpoint)
        keep = source.keep if source is not None else True
        label = source.label if source is not None else ""
        rebuilt.append(Segment(start, end, keep=keep, label=label))

    if not rebuilt and duration > 0:
        rebuilt.append(Segment(0, duration, keep=True))
    plan.segments = rebuilt
    return plan


def _segment_at(segments, pos_ms):
    """Segment contenant ``pos_ms`` (``start <= pos < end``), ou None."""
    for seg in segments:
        if seg.start_ms <= pos_ms < seg.end_ms:
            return seg
    return None


def split_at(plan, pos_ms):
    """Insère un point de coupe à ``pos_ms``. Sans effet si la position tombe sur
    une borne existante ou hors ``]0, duration[``. Retourne l'index du segment
    situé **à droite** de la coupe, ou -1 si aucune coupe n'a été créée."""
    pos_ms = int(pos_ms)
    if pos_ms <= 0 or pos_ms >= plan.duration_ms:
        return -1
    target = _segment_at(plan.segments, pos_ms)
    if target is None or pos_ms == target.start_ms:
        return -1

    idx = plan.segments.index(target)
    left = Segment(target.start_ms, pos_ms, keep=target.keep, label=target.label)
    right = Segment(pos_ms, target.end_ms, keep=target.keep, label=target.label)
    plan.segments[idx:idx + 1] = [left, right]
    return idx + 1


def remove_boundary(plan, seg_index):
    """Retire la coupe entre ``seg_index`` et son voisin de droite : les deux
    segments fusionnent (drapeau keep du segment de gauche conservé). Sans effet
    sur le dernier segment. Retourne True si une fusion a eu lieu."""
    if seg_index < 0 or seg_index >= len(plan.segments) - 1:
        return False
    left = plan.segments[seg_index]
    right = plan.segments[seg_index + 1]
    merged = Segment(left.start_ms, right.end_ms, keep=left.keep, label=left.label)
    plan.segments[seg_index:seg_index + 2] = [merged]
    return True


def set_keep(plan, seg_index, keep):
    if 0 <= seg_index < len(plan.segments):
        plan.segments[seg_index].keep = bool(keep)
        return True
    return False


def toggle_keep(plan, seg_index):
    if 0 <= seg_index < len(plan.segments):
        plan.segments[seg_index].keep = not plan.segments[seg_index].keep
        return True
    return False


def mark_region(plan, start_ms, end_ms, keep):
    """Applique un drapeau ``keep`` à l'intervalle ``[start_ms, end_ms]`` : pose
    les coupes aux deux bornes puis règle tous les segments couverts. Idiome des
    raccourcis « marquer début (S) / marquer fin (E) » de l'éditeur."""
    start_ms, end_ms = int(start_ms), int(end_ms)
    if end_ms < start_ms:
        start_ms, end_ms = end_ms, start_ms
    start_ms = max(0, start_ms)
    end_ms = min(plan.duration_ms, end_ms)
    if end_ms <= start_ms:
        return False
    split_at(plan, start_ms)
    split_at(plan, end_ms)
    changed = False
    for seg in plan.segments:
        if seg.start_ms >= start_ms and seg.end_ms <= end_ms:
            seg.keep = bool(keep)
            changed = True
    return changed


def set_segment_start(plan, index, new_ms):
    """Déplace la coupe **au début** du segment ``index`` (frontière avec le segment
    précédent) vers ``new_ms``, bornée pour rester ordonnée. Sans effet sur le
    premier segment (son début est 0). Retourne True si déplacé."""
    if index <= 0 or index >= len(plan.segments):
        return False
    left = plan.segments[index - 1]
    seg = plan.segments[index]
    lo, hi = left.start_ms + 1, seg.end_ms - 1
    if hi < lo:
        return False
    new_ms = max(lo, min(int(new_ms), hi))
    left.end_ms = new_ms
    seg.start_ms = new_ms
    return True


def set_segment_end(plan, index, new_ms):
    """Déplace la coupe **à la fin** du segment ``index`` (frontière avec le segment
    suivant) vers ``new_ms``, bornée. Sans effet sur le dernier segment. Retourne
    True si déplacé."""
    if index < 0 or index >= len(plan.segments) - 1:
        return False
    seg = plan.segments[index]
    right = plan.segments[index + 1]
    lo, hi = seg.start_ms + 1, right.end_ms - 1
    if hi < lo:
        return False
    new_ms = max(lo, min(int(new_ms), hi))
    seg.end_ms = new_ms
    right.start_ms = new_ms
    return True


def kept_regions(plan):
    """Régions gardées à exporter, en fusionnant les segments gardés **adjacents**
    (deux keep contigus → une seule pièce continue, pour des jointures propres).
    Retourne une liste de tuples ``(start_ms, end_ms)`` triés."""
    regions = []
    for seg in plan.segments:
        if not seg.keep or seg.duration_ms <= 0:
            continue
        if regions and regions[-1][1] == seg.start_ms:
            regions[-1] = (regions[-1][0], seg.end_ms)
        else:
            regions.append((seg.start_ms, seg.end_ms))
    return regions


def kept_duration_ms(plan):
    return sum(end - start for start, end in kept_regions(plan))


def plan_to_dict(plan):
    """Sérialise un plan (pour un fichier projet de découpe)."""
    return {
        'duration_ms': int(plan.duration_ms),
        'segments': [
            {'start_ms': int(s.start_ms), 'end_ms': int(s.end_ms),
             'keep': bool(s.keep), 'label': s.label or ''}
            for s in plan.segments
        ],
    }


def plan_from_dict(data, duration_ms=None):
    """Reconstruit un plan depuis un dict. Si ``duration_ms`` est fourni (durée du
    fichier réellement ouvert), le plan est reclampé/normalisé sur cette durée —
    utile pour rouvrir un projet sur le même fichier."""
    raw = data.get('segments') or []
    total = int(duration_ms if duration_ms is not None else data.get('duration_ms', 0) or 0)
    plan = SegmentPlan(duration_ms=total, segments=[])
    for entry in raw:
        try:
            start = int(entry.get('start_ms', 0))
            end = int(entry.get('end_ms', 0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        plan.segments.append(Segment(start, end, keep=bool(entry.get('keep', True)),
                                     label=str(entry.get('label', '') or '')))
    if not plan.segments and total > 0:
        plan.segments.append(Segment(0, total, keep=True))
    _normalize(plan)
    return plan


def validate(plan):
    """Retourne un message d'erreur (traduit) si le plan ne peut pas être exporté,
    sinon None. Un plan valide garde au moins une région de durée non nulle."""
    if plan.duration_ms <= 0:
        return _translate("This file has no known duration and cannot be split.")
    if not kept_regions(plan):
        return _translate("At least one segment must be kept.")
    return None
