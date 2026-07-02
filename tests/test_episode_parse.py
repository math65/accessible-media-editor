"""Tests for filename SxxExx / track parsing (core/episode_parse.py)."""

from core.episode_parse import parse_episode_from_filename, parse_track_from_filename


def test_sxxexx_token():
    info = parse_episode_from_filename("Show.Name.S01E02.720p.WEB.mkv")
    assert info == {"season": 1, "episode": 2, "show": "Show Name", "episode_title": "720p WEB"}


def test_lowercase_short_form():
    info = parse_episode_from_filename("show s1e2.mp4")
    assert info["season"] == 1
    assert info["episode"] == 2


def test_verbose_season_episode():
    info = parse_episode_from_filename("My Show Season 1 Episode 12.mp4")
    assert info["season"] == 1
    assert info["episode"] == 12


def test_x_form():
    info = parse_episode_from_filename("Show 1x02.mp4")
    assert info["season"] == 1
    assert info["episode"] == 2


def test_resolution_is_not_mistaken_for_episode():
    # 1280x720 must NOT parse as 1x02-style season/episode.
    assert parse_episode_from_filename("Movie.1280x720.BluRay.mkv") is None


def test_no_match_returns_none():
    assert parse_episode_from_filename("Just A Movie Title.mkv") is None
    assert parse_episode_from_filename("") is None


def test_track_leading_number():
    assert parse_track_from_filename("01 - Song Title.mp3") == {"track": 1, "disc": None}


def test_track_disc_pair():
    assert parse_track_from_filename("1-02 Song.flac") == {"track": 2, "disc": 1}


def test_track_delimited_by_spaced_hyphens():
    assert parse_track_from_filename("Artist - 03 - Title.mp3") == {"track": 3, "disc": None}


def test_track_ignores_stray_number_in_title():
    assert parse_track_from_filename("Three Blind Mice.mp3") is None
    assert parse_track_from_filename("Song With 5 Words.mp3") is None
