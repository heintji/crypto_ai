"""Twee processen tegelijk, tegen een ECHTE PostgreSQL (Astra + Fable, 20-9).

De dubbele-orderrem leunt op twee dingen die je alleen tegen een echte database
kunt bewijzen: een advisory lock (één run tegelijk) en een UNIQUE-constraint
(één positie per munt per dag). De bestaande tests gebruikten een nep-database,
dus je kon beide weghalen zonder dat er iets rood werd.
"""
import multiprocessing as mp
import uuid

import pytest

from trading.gate_live_store import Store

psycopg2 = pytest.importorskip("psycopg2")


@pytest.fixture
def db():
    try:
        beheer = psycopg2.connect("postgres:///postgres")
    except Exception:
        pytest.skip("geen lokale PostgreSQL")
    naam = f"gate_gelijk_{uuid.uuid4().hex[:10]}"
    beheer.autocommit = True
    with beheer.cursor() as c:
        c.execute(f"CREATE DATABASE {naam}")
    beheer.close()
    yield naam
    beheer = psycopg2.connect("postgres:///postgres")
    beheer.autocommit = True
    with beheer.cursor() as c:
        c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (naam,))
        c.execute(f"DROP DATABASE IF EXISTS {naam}")
    beheer.close()


def _store(naam):
    conn = psycopg2.connect(f"dbname={naam}")
    conn.autocommit = True
    return conn, Store(conn, "test")


def test_maar_een_run_krijgt_het_slot(db):
    conn1, store1 = _store(db)
    store1.install()
    conn2, store2 = _store(db)
    try:
        with store1.run_lock() as eerste:
            assert eerste is True
            with store2.run_lock() as tweede:
                assert tweede is False, "twee runs tegelijk zouden dubbel kunnen kopen"
        # na afloop moet het slot weer vrij zijn
        with store2.run_lock() as derde:
            assert derde is True
    finally:
        conn1.close()
        conn2.close()


def test_dezelfde_munt_op_dezelfde_dag_kan_maar_een_positie_hebben(db):
    conn1, store1 = _store(db)
    store1.install()
    conn2, store2 = _store(db)
    try:
        with store1.run_lock():
            eerste = store1.create("TEST_USDT", "2026-09-20", {"metadata": {}})
        assert eerste is not None
        with store2.run_lock():
            tweede = store2.create("TEST_USDT", "2026-09-20", {"metadata": {}})
        assert tweede is None, "tweede positie voor dezelfde munt op dezelfde dag"
        with conn1.cursor() as c:
            c.execute("SELECT count(*) FROM gate_v1_positions")
            assert c.fetchone()[0] == 1
    finally:
        conn1.close()
        conn2.close()


def _probeer_positie(naam, wortel, uitslag):
    """Draait in een APART proces: echte gelijktijdigheid, geen nagespeelde.

    Precies zoals de uitvoerder het doet: eerst het slot, dan de positie.
    """
    import sys
    sys.path.insert(0, wortel)
    import psycopg2 as pg
    from trading.gate_live_store import Store as EchteStore
    conn = pg.connect(f"dbname={naam}")
    conn.autocommit = True
    store = EchteStore(conn, "test")
    try:
        with store.run_lock() as slot:
            if not slot:
                uitslag.put(0)
                return
            import time
            time.sleep(0.2)          # overlap afdwingen
            positie = store.create("RACE_USDT", "2026-09-20", {"metadata": {}})
            uitslag.put(1 if positie else 0)
    except Exception as exc:  # noqa: BLE001
        print("kindproces:", exc, flush=True)
        uitslag.put(0)
    finally:
        conn.close()


def test_twee_echte_processen_maken_samen_een_positie(db):
    conn, store = _store(db)
    store.install()
    conn.close()
    uitslag = mp.get_context("spawn").Queue()
    ctx = mp.get_context("spawn")
    import os
    wortel = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    processen = [ctx.Process(target=_probeer_positie, args=(db, wortel, uitslag)) for _ in range(2)]
    for p in processen:
        p.start()
    for p in processen:
        p.join(timeout=30)
    gelukt = sum(uitslag.get() for _ in processen)
    assert gelukt == 1, f"{gelukt} processen maakten een positie; dat moet er precies één zijn"
