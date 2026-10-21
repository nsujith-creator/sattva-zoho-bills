"""A1: two genuinely overlapping watcher processes against an isolated lock file."""
import os,sys,tempfile,subprocess,time,pathlib,textwrap
SRC=pathlib.Path(__file__).resolve().parents[1]/"src"/"watcher.py"
CHILD=r'''
import importlib.util,sys,os,time
spec=importlib.util.spec_from_file_location("audit_watcher",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.LOCK_FILE=sys.argv[2]
m.LOG_FILE=sys.argv[3]
m.SYSTEM_HEALTH_FILE=sys.argv[4]
m.BILL_FOLDER=sys.argv[5]
def observed(*a,**k):
    with open(sys.argv[6],"a",encoding="utf-8") as f:f.write(str(os.getpid())+"\n")
    print("LOCK_ACQUIRED",flush=True)
    time.sleep(1.0)
    raise SystemExit(0)
m.write_watcher_heartbeat=observed
m.run_watcher()
'''
def run_case(stale):
    with tempfile.TemporaryDirectory(prefix="a1watcher") as t:
        p=pathlib.Path(t);lock=p/"watcher.lock"; winners=p/"winners.txt"
        if stale:lock.write_text("999999999",encoding="utf8")
        args=[sys.executable,"-c",CHILD,str(SRC),str(lock),str(p/"log.txt"),str(p/"health.json"),t,str(winners)]
        a=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        time.sleep(.08)
        b=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            ao,ae=a.communicate(timeout=15);bo,be=b.communicate(timeout=15)
        finally:
            if a.poll() is None:a.kill()
            if b.poll() is None:b.kill()
        content=winners.read_text(encoding="utf8").splitlines() if winners.exists() else []
        print("CASE",("STALE" if stale else "FRESH"),"WINNERS",content,"RETURNS",a.returncode,b.returncode,flush=True)
        assert len(content)==1,(content,ao,bo,ae,be)
        assert not (a.returncode and b.returncode),(ao,bo,ae,be)
if __name__=="__main__":
    run_case(False)
    run_case(True)
    print("A1 real-process concurrency PASSED")
