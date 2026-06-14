"""Tests for the Mongoose contacts export loader + caseload join.

Run: python tests/test_mongoose_contacts.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.mongoose_contacts import (  # noqa: E402
    build_contact_id_map, is_contacts_export, load_contacts,
)

HEADER = ("contactId,firstName,lastName,mobileNumber,optedOut,department,"
          "tags,Student Status,Course Instructor Name\n")
SAMPLE = HEADER + (
    "0033x000038J2X4AAK,Marquiece,Sims,18036535675,FALSE,C769,,AS,Jim Ashe\n"
    "0033x000038J2X5BBL,Paul,Green,13362132291,FALSE,C769,,AS,Jim Ashe\n"
    # blank mobile in caseload but present here -> joined by name
    "0033x000038J2X6CCM,Jane,Doe,,FALSE,C769,,AS,Jim Ashe\n"
    # opted out -> excluded by default
    "0033x000038J2X7DDN,Bob,Optout,15551112222,TRUE,C769,,AS,Jim Ashe\n"
    # duplicate name -> name fallback must NOT match it
    "0033x000038J2X8EEP,Dup,Name,15553334444,FALSE,C769,,AS,Jim Ashe\n"
    "0033x000038J2X9FFQ,Dup,Name,15555556666,FALSE,C769,,AS,Jim Ashe\n"
)


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def test_is_contacts_export():
    p = _write(SAMPLE)
    assert is_contacts_export(p)
    other = _write("StudentID,Name,MobilePhone\n123,Foo Bar,5551234567\n")
    assert not is_contacts_export(other)
    os.remove(p)
    os.remove(other)


def test_load_contacts():
    p = _write(SAMPLE)
    rows = load_contacts(p)
    os.remove(p)
    assert len(rows) == 6
    sims = rows[0]
    assert sims["contact_id"] == "0033x000038J2X4AAK"
    assert sims["mobile"] == "8036535675"  # 11-digit w/ leading 1 -> 10
    assert sims["opted_out"] is False
    assert rows[3]["opted_out"] is True  # Bob Optout


def test_join_by_mobile():
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S1", "Name": "Marquiece Sims",
                 "MobilePhone": "(803) 653-5675"}]
    m = build_contact_id_map(caseload, contacts)
    assert m == {"S1": "0033x000038J2X4AAK"}


def test_join_by_name_when_mobile_blank():
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S2", "Name": "Jane Doe", "MobilePhone": ""}]
    m = build_contact_id_map(caseload, contacts)
    assert m == {"S2": "0033x000038J2X6CCM"}


def test_opted_out_excluded():
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S3", "Name": "Bob Optout",
                 "MobilePhone": "5551112222"}]
    assert build_contact_id_map(caseload, contacts) == {}
    # included when asked
    m = build_contact_id_map(caseload, contacts, include_opted_out=True)
    assert m == {"S3": "0033x000038J2X7DDN"}


def test_duplicate_name_not_matched_by_name():
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    # blank mobile + ambiguous name -> no match (can't disambiguate)
    caseload = [{"StudentID": "S4", "Name": "Dup Name", "MobilePhone": ""}]
    assert build_contact_id_map(caseload, contacts) == {}
    # but a mobile match still works for a duplicate name
    caseload2 = [{"StudentID": "S4", "Name": "Dup Name",
                  "MobilePhone": "5553334444"}]
    assert build_contact_id_map(caseload2, contacts) == {
        "S4": "0033x000038J2X8EEP"}


def test_case_insensitive_name():
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S5", "Name": "jane DOE", "MobilePhone": ""}]
    assert build_contact_id_map(caseload, contacts) == {
        "S5": "0033x000038J2X6CCM"}


def test_join_loose_name_middle_name():
    # Caseload has a middle name + a different (blank) mobile; exact full-name
    # match fails but the first+last-word loose match should catch it.
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S6", "Name": "Marquiece Allen Sims",
                 "MobilePhone": ""}]
    assert build_contact_id_map(caseload, contacts) == {
        "S6": "0033x000038J2X4AAK"}


def test_loose_name_skips_ambiguous():
    # "Dup Name" appears twice -> loose match must NOT bind it either.
    contacts = load_contacts(_p := _write(SAMPLE))
    os.remove(_p)
    caseload = [{"StudentID": "S7", "Name": "Dup Middle Name",
                 "MobilePhone": ""}]
    assert build_contact_id_map(caseload, contacts) == {}


def test_persist_and_load_roundtrip():
    from src.mongoose_contacts import load_contact_ids, persist_contact_ids
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(db)  # let sqlite create it
    try:
        added, changed = persist_contact_ids(
            {"S1": "003AAA", "S2": "003BBB"}, db_path=db)
        assert (added, changed) == (2, 0)
        assert load_contact_ids(db_path=db) == {"S1": "003AAA", "S2": "003BBB"}
        # re-persist: one unchanged, one changed, one new
        added, changed = persist_contact_ids(
            {"S1": "003AAA", "S2": "003ZZZ", "S3": "003CCC"}, db_path=db)
        assert (added, changed) == (1, 1)
        assert load_contact_ids(db_path=db) == {
            "S1": "003AAA", "S2": "003ZZZ", "S3": "003CCC"}
    finally:
        if os.path.exists(db):
            os.remove(db)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
