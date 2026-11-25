"""Independent MacCI recovery channel. Only the fixed official repo and exact reviewed/tested PR candidates.
No arbitrary shell commands, no production changes, no self-modification of this trust anchor.
"""
import json
import fcntl
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory, NamedTemporaryFile
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT=Path.home()/".local/share/macmini-pr-tests"
REPO="layho/web-app-init-base-env"
REMOTE="https://github.com/"+REPO+".git"
SHA=re.compile(r"[0-9a-f]{40}\Z")
MARKER="[MACCI_RUNTIME_REVIEW] "
CONTEXT="macmini/runtime-install-v1"
OWNER="layho"

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None

def api(token,path,payload=None):
    req=Request("https://api.github.com/repos/"+REPO+path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization":"Bearer "+token,"Accept":"application/vnd.github+json",
                 "Content-Type":"application/json","User-Agent":"macci-runtime-recovery"})
    with build_opener(NoRedirect()).open(req,timeout=20) as response:
        raw=response.read(2_000_001)
        if len(raw)>2_000_000:
            raise ValueError("Oversized response")
        return json.loads(raw)

def private_read(path,limit=512):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"r",encoding="ascii") as stream:
        st=os.fstat(stream.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_size>limit:
            raise ValueError("Unsafe private file")
        return stream.read().strip()

def token_path():
    root=Path.home()/".local/share/macmini-github"
    try:
        info=root.lstat()
    except FileNotFoundError:
        return ROOT/"github-token"
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError("Unsafe shared credential directory")
    path=root/"github-token"
    try:
        path.lstat()
    except FileNotFoundError:
        return ROOT/"github-token"
    return path

def authorized(token,number):
    pr=api(token,f"/pulls/{number}")
    head=(pr.get("head") or {}).get("sha","")
    base=(pr.get("base") or {}).get("sha","")
    merge=pr.get("merge_commit_sha") or ""
    if not all(isinstance(x,str) and SHA.fullmatch(x) for x in (head,base,merge)):
        return None
    if (pr.get("state")!="open" or pr.get("user",{}).get("login")!=OWNER
        or pr.get("author_association")!="OWNER" or pr["base"].get("ref")!="preview"
        or any((pr[x].get("repo") or {}).get("full_name")!=REPO for x in ("head","base"))):
        return None
    if api(token,"/branches/preview")["commit"]["sha"]!=base:
        return None
    commit=api(token,"/git/commits/"+merge)
    if [p["sha"] for p in commit["parents"]]!=[base,head]:
        return None
    tree=commit["tree"]["sha"]
    if not SHA.fullmatch(tree):
        return None
    statuses=api(token,"/commits/"+head+"/statuses?per_page=100")
    latest=next((s for s in statuses if s.get("context")=="macmini/isolated-tests-v1"),None)
    if not latest or latest.get("state")!="success" or latest.get("creator",{}).get("login")!=OWNER or latest.get("description")!=f"base={base} merge={merge} passed":
        return None
    expected={"schema":1,"head":head,"base":base,"merge":merge,"tree":tree,"decision":"approved","scope":"macci-runtime"}
    comments=[]
    for page in range(1,11):
        rows=api(token,f"/issues/{number}/comments?per_page=100&page={page}")
        comments.extend(rows)
        if len(rows)<100:
            break
    else:
        return None
    for c in sorted(comments,key=lambda x:x["id"],reverse=True):
        body=c.get("body") or ""
        if c.get("author_association")!="OWNER" or c.get("user",{}).get("login")!=OWNER or not body.startswith(MARKER):
            continue
        try:
            data=json.loads(body[len(MARKER):])
        except ValueError:
            continue
        if isinstance(data,dict) and data.get("head")==head:
            return expected if data==expected else None
    return None

def save_receipt(head,merge):
    with NamedTemporaryFile(dir=ROOT,mode="w",encoding="ascii",delete=False) as out:
        path=Path(out.name)
        try:
            os.fchmod(out.fileno(),0o600)
            json.dump({"head":head,"merge":merge},out)
            out.flush()
            os.fsync(out.fileno())
            out.close()
            os.replace(path,ROOT/"runtime-receipt.pending")
        finally:
            path.unlink(missing_ok=True)

def flush_receipt(token):
    try:
        item=json.loads(private_read(ROOT/"runtime-receipt.pending"))
    except FileNotFoundError:
        return
    if not isinstance(item,dict) or set(item)!={"head","merge"} or not all(isinstance(x,str) and SHA.fullmatch(x) for x in item.values()):
        raise ValueError("Invalid receipt")
    if private_read(ROOT/"runtime-installed-sha")!=item["merge"]:
        raise ValueError("Receipt does not match installed runtime")
    desc=f'runtime={item["merge"]} installed'
    rows=api(token,"/commits/"+item["head"]+"/statuses?per_page=100")
    if not any(s.get("context")==CONTEXT and s.get("state")=="success" and s.get("description")==desc and (s.get("creator") or {}).get("login")==OWNER for s in rows):
        api(token,"/statuses/"+item["head"],{"state":"success","context":CONTEXT,"description":desc})
    (ROOT/"runtime-receipt.pending").unlink()

def run(token):
    if private_read(ROOT/"runtime-recovery.enabled")!="enabled":
        return
    flush_receipt(token)
    # Fixed explicit PR authorization, not execution of arbitrary issue text.
    rows=api(token,"/pulls?state=open&base=preview&per_page=100")
    if len(rows)>=100:
        raise ValueError("Too many candidates")
    for p in rows:
        number=p.get("number")
        if type(number) is not int or number<=0:
            continue
        approval=authorized(token,number)
        if approval is None:
            continue
        head,merge,tree=(approval[k] for k in ("head","merge","tree"))
        try:
            if private_read(ROOT/"runtime-installed-sha")==merge:
                save_receipt(head,merge)
                flush_receipt(token)
                continue
        except FileNotFoundError:
            pass
        with TemporaryDirectory(prefix="macci-recovery-") as folder:
            source=Path(folder)/"source"
            env={"PATH":os.environ["PATH"],"HOME":str(Path.home()),"GIT_TERMINAL_PROMPT":"0","GIT_CONFIG_NOSYSTEM":"1"}
            def git(*args):
                return subprocess.check_output(["git",*args],env=env,text=True,timeout=120).strip()
            git("init","-q",str(source))
            git("-C",str(source),"fetch","--no-tags",REMOTE,f"refs/pull/{number}/merge")
            git("-C",str(source),"checkout","--detach","FETCH_HEAD")
            if git("-C",str(source),"rev-parse","HEAD")!=merge or git("-C",str(source),"rev-parse","HEAD^{tree}")!=tree:
                raise ValueError("Candidate changed")
            # Installer is the locally pinned trusted copy, not downloaded code.
            if authorized(token,number)!=approval:
                raise ValueError("Authorization changed")
            installer=ROOT/"runtime-installer.py"
            private_read(installer,65536)
            subprocess.run([sys.executable,"-B",str(installer),"--source",str(source/"deploy/macci"),"--sha",merge,"--apply"],check=True,timeout=180,env=env)
        save_receipt(head,merge)
        flush_receipt(token)
        return

def main():
    if sys.platform!="darwin":
        return 1
    try:
        token=private_read(token_path())
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,300}",token):
            raise ValueError("Invalid credential")
        fd=os.open(ROOT/"runtime-recovery.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
        with os.fdopen(fd,"r+") as lock:
            st=os.fstat(lock.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077:
                raise ValueError("Unsafe recovery lock")
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            run(token)
        return 0
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        print("MacCI recovery unavailable; retry next tick",flush=True)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
