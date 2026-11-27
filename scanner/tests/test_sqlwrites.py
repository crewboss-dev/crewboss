"""Task #52: table names with digits, and an upsert's DO UPDATE SET is not a write to a table called SET."""
from scanner.lib.sqlwrites import written_tables


def test_digits_in_table_names_are_kept():
    assert written_tables("INSERT INTO audit2 (a) VALUES (1)") == ["audit2"]
    assert written_tables("update v2_events set x = 1") == ["v2_events"]
    assert written_tables("DELETE FROM  t1 WHERE id = 1") == ["t1"]


def test_upsert_is_one_write_not_two():
    sql = ("INSERT INTO syncro_assets (asset_id, name) VALUES (%s, %s) "
           "ON CONFLICT (asset_id) DO UPDATE SET name = EXCLUDED.name")
    assert written_tables(sql) == ["syncro_assets"]
    assert written_tables("INSERT INTO t (a) VALUES (1) ON CONFLICT DO\n  UPDATE SET a = 2") == ["t"]


def test_plain_update_and_dedupe():
    assert written_tables("UPDATE cases SET x = 1; UPDATE cases SET y = 2; INSERT INTO notes VALUES (1)") \
        == ["cases", "notes"]
    assert written_tables("SELECT * FROM cases") == []
