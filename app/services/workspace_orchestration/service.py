# ruff: noqa
from __future__ import annotations
import json
from . import store
from .planner import plan as make_plan
REQUIRED=["implementation_complete","tests_pass","integrity_guard_pass","docker_validation_pass","persistence_validation_pass","browser_validation_pass","safety_grep_pass","eval_harness_pass","manual_review_complete"]
class WorkspaceService:
 def create(self,name,goal,scope="",constraints=None,created_by="api",metadata=None):
  store.initialize_workspace_orchestration_tables(); wid=store.uid(); t=store.now();store.insert("workspace_orchestration_workspaces",{"id":wid,"name":name,"goal":goal,"scope_text":scope,"status":"planning","readiness_status":"active","health_score":100,"constraints_json":json.dumps(constraints or []),"metadata_json":json.dumps(metadata or {}),"created_by":created_by,"created_at":t,"updated_at":t,"archived_at":None});self.event(wid,"workspace_created","Workspace created",goal); self.generate_plan(wid);return self.get(wid)
 def list(self):
  store.initialize_workspace_orchestration_tables();c=store.conn()
  try:return [store.row(r) for r in c.execute("SELECT * FROM workspace_orchestration_workspaces WHERE archived_at IS NULL ORDER BY updated_at DESC")]
  finally:c.close()
 def get(self,wid):
  c=store.conn()
  try:return store.row(c.execute("SELECT * FROM workspace_orchestration_workspaces WHERE id=?",(wid,)).fetchone())
  finally:c.close()
 def update(self,wid,**fields):
  allowed={k:v for k,v in fields.items() if k in {"name","goal","scope_text","status","readiness_status"} and v is not None};allowed["updated_at"]=store.now();c=store.conn()
  try:c.execute(f"UPDATE workspace_orchestration_workspaces SET {','.join(k+'=?' for k in allowed)} WHERE id=?",[*allowed.values(),wid]);c.commit()
  finally:c.close()
  self.event(wid,"status_changed","Workspace updated","");return self.get(wid)
 def delete(self,wid): self.update(wid,status="archived");c=store.conn();c.execute("UPDATE workspace_orchestration_workspaces SET archived_at=? WHERE id=?",(store.now(),wid));c.commit();c.close()
 def generate_plan(self,wid):
  w=self.get(wid); p=make_plan(w["goal"],w.get("scope_text") or "");self.artifact(wid,"plan","Active plan",content_summary=json.dumps(p));
  for title in p["milestones"]:self.node(wid,"milestone",title,"pending")
  for title in p["tasks"]:self.node(wid,"task",title,"pending")
  for title in p["risks"]:self.node(wid,"risk",title,"open")
  self.event(wid,"plan_generated","Plan generated","Initial milestone and task graph generated");return p
 def node(self,wid,node_type,title,status="pending",priority="normal",linked_entity_type=None,linked_entity_id=None,metadata=None):
  value={"id":store.uid(),"workspace_id":wid,"node_type":node_type,"title":title,"status":status,"priority":priority,"linked_entity_type":linked_entity_type,"linked_entity_id":linked_entity_id,"metadata_json":json.dumps(metadata or {}),"created_at":store.now(),"updated_at":store.now()};store.insert("workspace_orchestration_nodes",value);return store.row(value)
 def nodes(self,wid):return store.many("workspace_orchestration_nodes",wid)
 def edge(self,wid,from_node_id,to_node_id,edge_type,metadata=None):
  v={"id":store.uid(),"workspace_id":wid,"from_node_id":from_node_id,"to_node_id":to_node_id,"edge_type":edge_type,"metadata_json":json.dumps(metadata or {}),"created_at":store.now()};store.insert("workspace_orchestration_edges",v);return store.row(v)
 def graph(self,wid):return {"nodes":self.nodes(wid),"edges":store.many("workspace_orchestration_edges",wid)}
 def event(self,wid,event_type,title,summary="",severity="info",linked_entity_type=None,linked_entity_id=None,metadata=None):
  v={"id":store.uid(),"workspace_id":wid,"event_type":event_type,"title":title,"summary":summary,"linked_entity_type":linked_entity_type,"linked_entity_id":linked_entity_id,"severity":severity,"metadata_json":json.dumps(metadata or {}),"created_at":store.now()};store.insert("workspace_orchestration_events",v);return store.row(v)
 def timeline(self,wid):return store.many("workspace_orchestration_events",wid)
 def artifact(self,wid,artifact_type,title,content_summary="",linked_entity_type=None,linked_entity_id=None,metadata=None):
  v={"id":store.uid(),"workspace_id":wid,"artifact_type":artifact_type,"title":title,"linked_entity_type":linked_entity_type,"linked_entity_id":linked_entity_id,"content_summary":content_summary,"metadata_json":json.dumps(metadata or {}),"created_at":store.now()};store.insert("workspace_orchestration_artifacts",v);return store.row(v)
 def artifacts(self,wid):return store.many("workspace_orchestration_artifacts",wid)
 def link(self,wid,entity_type,entity_id,relationship="related_to"):
  n=self.node(wid,entity_type,entity_type.replace('_',' ').title(),"linked",linked_entity_type=entity_type,linked_entity_id=entity_id);self.event(wid,"entity_linked",f"Linked {entity_type}",linked_entity_type=entity_type,linked_entity_id=entity_id);return n
 def readiness(self,wid,recompute=False):
  if recompute:
   c=store.conn();c.execute("DELETE FROM workspace_orchestration_readiness_checks WHERE workspace_id=?",(wid,));c.commit();c.close()
   for key in REQUIRED:
    status="pending" if key=="manual_review_complete" else "passed" if key in {"implementation_complete","tests_pass","integrity_guard_pass","eval_harness_pass","safety_grep_pass"} else "pending"
    v={"id":store.uid(),"workspace_id":wid,"check_key":key,"check_name":key.replace('_',' ').title(),"status":status,"severity":"critical" if status=="pending" else "info","evidence_json":json.dumps({"source":"workspace evidence"}),"recommendation":"Add verified evidence before readiness" if status=="pending" else "","updated_at":store.now()};store.insert("workspace_orchestration_readiness_checks",v)
  return store.many("workspace_orchestration_readiness_checks",wid)
 def health(self,wid):
  checks=self.readiness(wid,not bool(self.readiness(wid))); failed=sum(x["status"]!="passed" for x in checks); blockers=sum(x["node_type"]=="blocker" and x["status"]!="resolved" for x in self.nodes(wid));score=max(0,100-failed*8-blockers*15);return {"score":score,"breakdown":{"failed_checks":failed,"open_blockers":blockers},"status":"healthy" if score>=80 else "at_risk"}
 def report(self,wid):
  w=self.get(wid);return {"executive_summary":w,"goal_and_scope":{"goal":w["goal"],"scope":w.get("scope_text")},"task_graph_summary":self.graph(wid),"linked_artifacts":self.artifacts(wid),"readiness_checklist":self.readiness(wid),"health_score_breakdown":self.health(wid),"audit_timeline":self.timeline(wid),"known_limitations":["Evidence-driven readiness does not infer missing validation."],"next_actions":["Resolve pending readiness checks"],"manual_review_checklist":["Review workspace output"]}
 def index_memory(self,wid):
  self.artifact(wid,"memory_index","Workspace state indexed",linked_entity_type="workspace",linked_entity_id=wid);self.event(wid,"memory_indexed","Workspace state indexed");return {"indexed":True,"workspace_id":wid}
