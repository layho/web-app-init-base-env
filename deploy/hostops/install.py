"""Local operator-only installation of an independently verified HostOps release.

No downloads, sudo, remote policy changes or other service manipulation.
"""
import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

MODULES=("agent.py","diagnostics.py","protocol.py")
LABEL="com.layho.hostops.agent"


def private_directory(path):
    path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError("unsafe private directory")


def read_private(path,limit=512):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_size>limit:
            raise ValueError("unsafe private file")
        return stream.read(limit+1)


def verified_files(source,expected):
    if not re.fullmatch(r"[0-9a-f]{64}",expected):
        raise ValueError("independently obtained release digest required")
    files={}
    for name in MODULES:
        fd=os.open(source/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,"rb") as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size>262144:
                raise ValueError("invalid release file")
            data=stream.read(262145)
            if len(data)>262144:
                raise ValueError("release file too large")
            compile(data,name,"exec")
            files[name]=data
    manifest={name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    if digest!=expected:
        raise ValueError("release digest mismatch")
    return files


def stage(root,source,digest):
    private_directory(root)
    versions=root/"versions"
    private_directory(versions)
    target=versions/digest
    files=verified_files(source,digest)
    if target.exists() or target.is_symlink():
        private_directory(target)
        verified_files(target,digest)
        return target
    temp=Path(tempfile.mkdtemp(prefix=".stage-",dir=versions))
    try:
        for name,data in files.items():
            with (temp/name).open("xb") as stream:
                stream.write(data)
            (temp/name).chmod(0o600)
        temp.rename(target)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return target


def launch_spec(root,release,host,origin,python,dispatch=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}",host):
        raise ValueError("invalid host identity")
    url=urlsplit(origin)
    if url.scheme!="https" or not url.hostname or url.username is not None or url.password is not None or url.path not in ("","/") or url.query or url.fragment or any(c.isspace() for c in origin):
        raise ValueError("HTTPS origin required")
    if not python.is_absolute() or not python.is_file() or not os.access(python,os.X_OK):
        raise ValueError("absolute executable Python required")
    state=root/"state"
    private_directory(state)
    token=read_private(root/"agent.token").decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}",token):
        raise ValueError("invalid agent token")
    env={"HOSTOPS_HOST_ID":host,"HOSTOPS_GATEWAY_ORIGIN":origin.rstrip("/"),
         "HOSTOPS_AGENT_TOKEN_FILE":str(root/"agent.token"),"PYTHONDONTWRITEBYTECODE":"1"}
    if dispatch:
        key=read_private(root/"job.key").decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{64}",key) or key==token:
            raise ValueError("invalid independent signing key")
        env.update(HOSTOPS_AGENT_DISPATCH="true",HOSTOPS_AGENT_STATE_DIR=str(state),
                   HOSTOPS_JOB_KEY_FILE=str(root/"job.key"))
    return {"Label":LABEL,"ProgramArguments":[str(python),str(release/"agent.py")],
            "EnvironmentVariables":env,"WorkingDirectory":str(root),
            "RunAtLoad":True,"KeepAlive":{"SuccessfulExit":False},
            "ThrottleInterval":30,"ProcessType":"Background",
            "StandardOutPath":str(root/"agent.log"),"StandardErrorPath":str(root/"agent.log")}


def atomic_file(path,data):
    fd,name=tempfile.mkstemp(prefix=".hostops-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_launchctl(*args):
    return subprocess.run(["/bin/launchctl",*args],stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                          timeout=20,check=False).returncode


def activate(plist,spec,runner=run_launchctl,probe=None):
    domain="gui/"+str(os.getuid())
    service=domain+"/"+LABEL
    previous=None
    if plist.exists() or plist.is_symlink():
        previous=read_private(plist,65536)
        old=plistlib.loads(previous)
        if old.get("Label")!=LABEL:
            raise ValueError("refusing to replace another service")
    loaded=runner("print",service)==0
    if loaded and previous is None:
        raise ValueError("existing service has no managed plist")
    if loaded and runner("bootout",service)!=0:
        raise RuntimeError("existing service could not be stopped")
    try:
        atomic_file(plist,plistlib.dumps(spec))
        if runner("bootstrap",domain,str(plist))!=0 or runner("print",service)!=0:
            raise RuntimeError("service registration failed")
        if probe is not None and not probe(spec):
            raise RuntimeError("heartbeat probe failed")
    except Exception:
        if runner("print",service)==0 and runner("bootout",service)!=0:
            raise RuntimeError("new service could not be stopped; manual recovery required") from None
        if previous is None:
            plist.unlink(missing_ok=True)
        else:
            atomic_file(plist,previous)
            if loaded and (runner("bootstrap",domain,str(plist))!=0 or runner("print",service)!=0):
                raise RuntimeError("previous service restoration not confirmed") from None
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--digest",required=True)
    parser.add_argument("--host",required=True)
    parser.add_argument("--origin",required=True)
    parser.add_argument("--dispatch",action="store_true")
    parser.add_argument("--activate",action="store_true")
    args=parser.parse_args()
    if sys.platform!="darwin" or os.getuid()==0:
        parser.error("run as the dedicated non-root macOS login user")
    root=Path.home()/".hostops"
    try:
        release=stage(root,args.source,args.digest)
        spec=launch_spec(root,release,args.host,args.origin,Path(sys.executable),args.dispatch)
        if not args.activate:
            print("Verified release staged; service not changed")
            return 0
        launch_dir=Path.home()/"Library/LaunchAgents"
        launch_dir.mkdir(exist_ok=True)
        info=launch_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o022:
            raise ValueError("unsafe LaunchAgents directory")
        # Check the exact staged code against the gateway before changing the service.
        # Never dispatch a task during installation probes.
        env={**spec["EnvironmentVariables"],"HOSTOPS_AGENT_DISPATCH":"false",
             "PATH":"/usr/bin:/bin","HOME":str(Path.home())}
        def probe(_):
            return subprocess.run([*spec["ProgramArguments"],"--once"],env=env,
                                  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,timeout=35,check=False).returncode==0
        if not probe(spec):
            raise RuntimeError("staged release heartbeat probe failed; service unchanged")
        activate(launch_dir/(LABEL+".plist"),spec,probe=probe)
        print("HostOps LaunchAgent registered; independent runtime heartbeat verification required")
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError):
        print("HostOps installation not confirmed; check local configuration and service state",file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
