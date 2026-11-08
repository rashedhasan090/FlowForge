"""Database layer: PostgreSQL (Vultr Managed Database) when DATABASE_URL is set, else local SQLite. Includes a bounded connection pool
and an audit writer that serialises hash-chain appends across app nodes with an advisory lock."""
import hashlib, os, sqlite3, threading
DSN = os.getenv("DATABASE_URL", "")
PG = bool(DSN)
if PG:
    try: import psycopg2
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'psycopg2-binary'], check=True)
        import psycopg2
SQLITE = os.getenv("DB_PATH", "flowforge.db")
_sem = threading.BoundedSemaphore(int(os.getenv("DB_MAX", "20")))
_free = []; _lock = threading.Lock()

def jt(f): return f"(data::json->>'{f}')" if PG else f"json_extract(data,'$.{f}')"
def jn(f): return f"((data::json->>'{f}')::float8)" if PG else f"json_extract(data,'$.{f}')"

class Row(dict):
    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)

class Cur:
    def __init__(s, cur): s.cur = cur
    def _r(s, r): return Row(zip([d[0] for d in s.cur.description], r)) if r is not None else None
    def fetchone(s): return s._r(s.cur.fetchone())
    def fetchall(s): return [s._r(r) for r in s.cur.fetchall()]
    def __iter__(s): return iter(s.fetchall())

def _new():
    if PG:
        c = psycopg2.connect(DSN, connect_timeout=10); return c
    c = sqlite3.connect(SQLITE, timeout=20, check_same_thread=False); return c

class Conn:
    def __init__(s, raw): s.raw = raw; s.pending = []; s.done = False
    def execute(s, sql, params=()):
        cur = s.raw.cursor(); cur.execute(sql.replace("?", "%s") if PG else sql, params); return Cur(cur)
    def executemany(s, sql, seq):
        cur = s.raw.cursor(); cur.executemany(sql.replace("?", "%s") if PG else sql, seq)
    def commit(s):
        if s.pending:
            if PG: s.execute("SELECT pg_advisory_xact_lock(4242)")
            r = s.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone(); prev = r["hash"] if r else "GENESIS"
            for rid, ts, agent, msg, d in s.pending:
                h = hashlib.sha256(f"{prev}|{rid}|{ts!r}|{agent}|{msg}|{d}".encode()).hexdigest()
                s.execute("INSERT INTO audit(req,ts,agent,msg,detail,prev,hash) VALUES(?,?,?,?,?,?,?)", (rid, ts, agent, msg, d, prev, h)); prev = h
            s.pending = []
        s.raw.commit()
    def close(s):
        if s.done: return
        s.done = True
        try: s.raw.rollback(); ok = not getattr(s.raw, "closed", 0)
        except Exception: ok = False
        if ok:
            with _lock: _free.append(s.raw)
        _sem.release()
    __del__ = close

def connect():
    _sem.acquire()
    try:
        raw = None
        with _lock: raw = _free.pop() if _free else None
        if raw is not None and PG:
            try: raw.cursor().execute("SELECT 1"); raw.rollback()
            except Exception: raw = None
        return Conn(raw or _new())
    except Exception:
        _sem.release(); raise

SCHEMA = ["CREATE TABLE IF NOT EXISTS req(id TEXT PRIMARY KEY, created REAL, raw TEXT, data TEXT, status TEXT, approver_role TEXT, rule_id TEXT, decided_by TEXT, decided_at REAL)",
          "CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, req TEXT, ts REAL, agent TEXT, msg TEXT, detail TEXT, prev TEXT, hash TEXT)",
          "CREATE TABLE IF NOT EXISTS vendors(name TEXT PRIMARY KEY, status TEXT, lim REAL, country TEXT)",
          "CREATE TABLE IF NOT EXISTS policy(v INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, rules TEXT, note TEXT)",
          "CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)",
          "CREATE INDEX IF NOT EXISTS audit_req ON audit(req)", "CREATE INDEX IF NOT EXISTS req_created ON req(created)"]

def init():
    c = connect()
    try:
        if PG: c.execute("SELECT pg_advisory_lock(1)")
        for s in SCHEMA:
            if PG: s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY").replace("REAL", "DOUBLE PRECISION")
            c.execute(s)
        c.commit()
        if PG: c.execute("SELECT pg_advisory_unlock(1)"); c.commit()
    finally: c.close()
