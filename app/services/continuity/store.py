# ruff: noqa
import sqlite3,json,uuid
from datetime import datetime,UTC
from app.core.config import get_settings
def now():return datetime.now(UTC).isoformat()
def uid():return str(uuid.uuid4())
def conn():
 u=get_settings().database_url;c=sqlite3.connect(u.replace("sqlite:///","",1) if u.startswith("sqlite:///") else "neo_memory.db");c.row_factory=sqlite3.Row;return c
def initialize_continuity_tables():
 c=conn();c.executescript("""CREATE TABLE IF NOT EXISTS workspace_continuity_bundles (id TEXT PRIMARY KEY,bundle_name TEXT NOT NULL,bundle_type TEXT NOT NULL,status TEXT NOT NULL,root_entity_type TEXT,root_entity_id TEXT,manifest_json TEXT NOT NULL,summary_text TEXT,integrity_hash TEXT,export_path TEXT,import_source TEXT,redaction_summary_json TEXT,created_by TEXT,created_at TEXT NOT NULL,completed_at TEXT,error TEXT);CREATE TABLE IF NOT EXISTS workspace_continuity_references (id TEXT PRIMARY KEY,bundle_id TEXT,root_entity_type TEXT,root_entity_id TEXT,source_entity_type TEXT NOT NULL,source_entity_id TEXT NOT NULL,target_entity_type TEXT NOT NULL,target_entity_id TEXT NOT NULL,relationship TEXT NOT NULL,status TEXT NOT NULL,metadata_json TEXT,created_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS workspace_continuity_validation_results (id TEXT PRIMARY KEY,bundle_id TEXT,validation_type TEXT NOT NULL,status TEXT NOT NULL,severity TEXT,title TEXT NOT NULL,details TEXT,evidence_json TEXT,recommendation TEXT,created_at TEXT NOT NULL);CREATE INDEX IF NOT EXISTS idx_cont_bundle ON workspace_continuity_references(bundle_id);CREATE INDEX IF NOT EXISTS idx_cont_validation ON workspace_continuity_validation_results(bundle_id);""");c.commit();c.close()
def row(r):
 if not r:return None
 d=dict(r)
 for k in list(d):
  if k.endswith('_json'):d[k[:-5]]=json.loads(d.pop(k) or '{}')
 return d
def rows(table,bid=None):
 c=conn();q=f"SELECT * FROM {table}"+(" WHERE bundle_id=?" if bid else "")+" ORDER BY created_at";v=[row(x) for x in c.execute(q,(bid,) if bid else ())];c.close();return v
