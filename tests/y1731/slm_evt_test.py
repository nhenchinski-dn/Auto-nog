#!/usr/bin/env python3
import paramiko, time, re
ANSI = re.compile(r"\[[0-9;]*[A-Za-z]")
def rs(c,cmds,t=30):
 ch=c.invoke_shell();ch.settimeout(t);time.sleep(1.5)
 while ch.recv_ready():ch.recv(65536)
 R=[]
 for cmd in cmds:
  ch.send(cmd+chr(10));o="";et=time.time()+t;ld=time.time()
  while time.time()<et:
   if ch.recv_ready():o+=ch.recv(65536).decode(errors="ignore");ld=time.time()
   else:
    if time.time()-ld>3:break
    time.sleep(0.2)
  R.append((cmd,ANSI.sub("",o)))
 ch.close();return R
c=paramiko.SSHClient();c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("WKY1C7VD00008P2",username="dnroot",password="dnroot",timeout=15,banner_timeout=15,auth_timeout=15)
lc=c.invoke_shell();lc.settimeout(5);time.sleep(1.5)
while lc.recv_ready():lc.recv(65536)
lc.send("set logging terminal"+chr(10));time.sleep(2)
while lc.recv_ready():lc.recv(65536)
print("Log open")
P="SLM_EVT_P2";S="SLM_EVT_S2"
rs(c,["configure","services performance-monitoring profiles cfm two-way-synthetic-loss-measurement "+P,"pcp 5","inform-test-results enabled","test-duration probes probe-count 3 probe-interval 1 repeat-interval 5","thresholds far-end-loss 0.01","exit","exit","exit","exit","exit","services performance-monitoring cfm two-way-synthetic-loss-measurement "+S,"admin-state enabled","profile "+P,"source maintenance-domain MD-CUST1 maintenance-association MA-CUST1 mep-id 4","target mep-id 3","exit","exit","exit","exit","commit","exit"],t=45)
print("Created. Wait 25s...")
time.sleep(25)
eo=""
try:
 lc.settimeout(2)
 while True:
  try:
   d=lc.recv(65536).decode(errors="ignore")
   if not d:break
   eo+=d
  except:break
except:pass
eo=ANSI.sub("",eo);print("Captured",len(eo),"bytes")
if "SYNTHETIC_LOSS" in eo or "FAR_END" in eo:print("RESULT: SLM threshold event!")
elif "CFM_PROACTIVE_TEST_FAILURE" in eo:print("RESULT: CFM event found")
else:print("RESULT: No SLM event - BUG")
for ln in eo.splitlines():
 if "CFM_PROACTIVE" in ln:print("  EVENT:",ln.strip()[:200])
if "CFM_PROACTIVE" not in eo:print("  Raw:",eo[:400])
rs(c,["configure","no services performance-monitoring cfm two-way-synthetic-loss-measurement "+S,"no services performance-monitoring profiles cfm two-way-synthetic-loss-measurement "+P,"commit","exit"],t=30)
lc.close();c.close();print("DONE")
