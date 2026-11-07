"""Runnable checks for triage classification. Run: python tests/test_triage.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from importarr.triage import classify_candidates


def test_timing_when_no_candidates():
    v = classify_candidates([])
    assert v.cls == "timing"
    assert v.candidates == []


def test_env_when_disk_rejection():
    v = classify_candidates([
        {"path": "/a/01.flac",
         "rejections": [{"reason": "Disk is not writable", "type": "permanent"}]}])
    assert v.cls == "env"
    assert "Disk is not writable" in v.reason


def test_mapping_when_track_count_mismatch():
    v = classify_candidates([
        {"path": "/a/01.flac",
         "rejections": [{"reason": "Too few tracks: album has 10, found 2",
                         "type": "permanent"}]}])
    assert v.cls == "mapping"


def test_clean_when_no_rejections():
    v = classify_candidates([
        {"path": "/a/01.flac", "rejections": []},
        {"path": "/a/02.flac", "rejections": []}])
    assert v.cls == "clean"
    assert len(v.candidates) == 2
    assert v.rejected == []


def test_env_wins_over_mapping():
    v = classify_candidates([
        {"path": "/a/01.flac",
         "rejections": [{"reason": "Too few tracks", "type": "permanent"}]},
        {"path": "/a/02.flac",
         "rejections": [{"reason": "not writable", "type": "permanent"}]}])
    assert v.cls == "env"


def test_reasons_deduped():
    v = classify_candidates([
        {"path": "/a/01.flac", "rejections": [{"reason": "dup", "type": "temporary"}]},
        {"path": "/a/02.flac", "rejections": [{"reason": "dup", "type": "temporary"}]}])
    assert v.reasons.count("dup") == 2  # reasons keep all; dedup is display-only
    assert v.reason.count("dup") == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} passed")
