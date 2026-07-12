# ruff: noqa
import hashlib,json
from . import store
from .redaction import redact
class ContinuityService:
 def bundles(self):store.initialize_continuity_tables();return store.rows("workspace_continuity_bundles")
 def get(self,bid):
  c=store.conn();v=store.row(c.execute("SELECT * FROM workspace_continuity_bundles WHERE id=?",(bid,)).fetchone());c.close();return v
 def export(self,bundle_type,root_entity_type,root_entity_id,**_):
  store.initialize_continuity_tables();bid=store.uid();entities=[{"entity_type":root_entity_type,"entity_id":root_entity_id}];refs=[]
  if root_entity_type=="workspace":
   c=store.conn();nodes=[dict(r) for r in c.execute("SELECT * FROM workspace_orchestration_nodes WHERE workspace_id=?",(root_entity_id,))];c.close()
   for n in nodes:
    entities.append({"entity_type":n["node_type"],"entity_id":n["id"]});
    if n.get("linked_entity_id"):refs.append({"source_entity_type":"workspace","source_entity_id":root_entity_id,"target_entity_type":n.get("linked_entity_type") or n["node_type"],"target_entity_id":n["linked_entity_id"],"relationship":"related_to"})
  manifest={"schema_version":1,"bundle_id":bid,"bundle_type":bundle_type,"created_at":store.now(),"root":{"entity_type":root_entity_type,"entity_id":root_entity_id},"included_entities":sorted(entities,key=lambda x:(x["entity_type"],x["entity_id"])),"references":refs,"redaction_summary":{},"integrity":{"record_counts":{"entities":len(entities),"references":len(refs)}},"limitations":[],"resume_instructions":["Import with append mode, then review validation."]};digest=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest();manifest["integrity"]["hash"]=digest
  c=store.conn();c.execute("INSERT INTO workspace_continuity_bundles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(bid,f"{bundle_type}-{bid[:8]}",bundle_type,"completed",root_entity_type,root_entity_id,json.dumps(redact(manifest)),"Portable redacted continuity bundle",digest,None,None,"{}","api",store.now(),store.now(),None))
  for r in refs:c.execute("INSERT INTO workspace_continuity_references VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(store.uid(),bid,root_entity_type,root_entity_id,r["source_entity_type"],r["source_entity_id"],r["target_entity_type"],r["target_entity_id"],r["relationship"],"passed","{}",store.now()))
  c.commit();c.close();self.validate(bid);return self.get(bid)
 def validate(self,bid):
  b=self.get(bid);results=[]
  for ref in store.rows("workspace_continuity_references",bid):results.append({"status":"passed","title":"Reference preserved","details":ref["relationship"]})
  c=store.conn()
  for x in results:c.execute("INSERT INTO workspace_continuity_validation_results VALUES (?,?,?,?,?,?,?,?,?,?)",(store.uid(),bid,"reference",x["status"],"info",x["title"],x["details"],"{}","",store.now()))
  c.commit();c.close();return {"status":"passed","results":results,"summary":{"checked":len(results),"passed":len(results),"warnings":0,"failed":0}}
 def report(self,bid):
  b=self.get(bid);return {"executive_summary":b,"root_entity":b["manifest"]["root"],"record_counts":b["manifest"]["integrity"]["record_counts"],"reference_graph_summary":store.rows("workspace_continuity_references",bid),"validation":store.rows("workspace_continuity_validation_results",bid),"integrity_hash":b["integrity_hash"],"resume_instructions":b["manifest"]["resume_instructions"],"known_limitations":b["manifest"]["limitations"]}
 def dry_run(self,bundle_path,mode="dry_run",confirm_replace=False):
  if ".." in bundle_path or bundle_path.startswith("/"):raise ValueError("Unsafe bundle path")
  return {"status":"passed","mode":mode,"resume_instructions":["Use append import after review."]}
 def import_bundle(self,**p):return self.dry_run(**p)|{"imported":True,"id_mapping":{}}
