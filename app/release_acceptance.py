"""Submit exact-SHA owner acceptance requests; the release coordinator publishes main.

The web service intentionally has no Contents-write or PR-merge credential.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

REPO = "layho/web-app-init-base-env"
CONTEXT = "macmini/isolated-tests-v1"
RECEIPTS = Path(__file__).resolve().parents[1] / "logs" / "release-acceptance"
SHA = re.compile(r"[0-9a-f]{40}\Z")


class AcceptanceError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def github_token():
    """Use one configured GitHub credential for release REST and GraphQL.

    APP_GITHUB_TOKEN is the preferred single-token setting. Legacy variable
    names remain supported for existing installations, with PR token taking
    precedence to avoid selecting an old Issues-only credential.
    """
    return (os.getenv("APP_GITHUB_TOKEN", "").strip()
            or os.getenv("APP_GITHUB_PR_TOKEN", "").strip()
            or os.getenv("APP_GITHUB_FEEDBACK_TOKEN", "").strip())


def enabled():
    return (os.getenv("APP_BASE_PREVIEW") == "1"
            and os.getenv("APP_PUBLIC_ORIGIN") == "https://base.layho.com"
            and bool(github_token()))


def github(path, data=None):
    token = github_token()
    if not token:
        raise AcceptanceError("GH-TOKEN-MISSING：运行服务未配置 GitHub Token")
    request = Request("https://api.github.com" + path,
                      data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                               "Content-Type": "application/json", "User-Agent": "base-release-acceptance"})
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise AcceptanceError(
                f"GH-REST-{exc.code}：当前 GitHub Token 被拒绝；"
                "请核查运行服务的凭据、仓库授权和对应 API 权限") from None
        raise AcceptanceError(f"GH-REST-{exc.code}：GitHub 请求失败") from None
    except (URLError, OSError, ValueError):
        raise AcceptanceError("GH-REST-NETWORK：GitHub 请求失败或回执无效") from None

def api(path, data=None):
    return github(f"/repos/{REPO}{path}", data)


def valid_sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def ref(name):
    sha = api(f"/branches/{name}")["commit"]["sha"]
    if not valid_sha(sha):
        raise AcceptanceError("GitHub 分支 SHA 无效")
    return sha


def test_evidence(candidate):
    commit = api(f"/git/commits/{candidate}")
    parents = [p["sha"] for p in commit["parents"]]
    if len(parents) != 2 or not all(valid_sha(p) for p in parents):
        raise AcceptanceError("当前版本缺少经过 MacCI 验证的合并候选")
    base, head = parents
    statuses = api(f"/commits/{head}/statuses?per_page=100")
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if not latest or latest.get("state") != "success":
        raise AcceptanceError("当前候选尚无有效 MacCI 成功回执")
    match = re.fullmatch(r"base=([0-9a-f]{40}) merge=([0-9a-f]{40}) passed", latest.get("description", ""))
    if not match or match[1] != base:
        raise AcceptanceError("测试回执与候选基线不一致")
    tested = api(f"/git/commits/{match[2]}")
    if [p["sha"] for p in tested["parents"]] != parents or tested["tree"]["sha"] != commit["tree"]["sha"]:
        raise AcceptanceError("当前运行代码与测试通过的代码不一致")
    return {"test_head": head, "test_base": base, "tested_merge": match[2], "tree": commit["tree"]["sha"]}


def contains(ancestor, descendant):
    if ancestor == descendant:
        return True
    comparison = api(f"/compare/{ancestor}...{descendant}")
    return comparison.get("status") in {"ahead", "identical"}


def receipt_path(sha):
    if not valid_sha(sha):
        raise AcceptanceError("无效候选 SHA")
    return RECEIPTS / (sha + ".json")


def load_receipt(sha):
    try:
        return json.loads(receipt_path(sha).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        raise AcceptanceError("本机验收记录读取失败") from None


def save_receipt(sha, data):
    path = receipt_path(sha)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=RECEIPTS, delete=False) as temp:
        try:
            json.dump(data, temp, ensure_ascii=False)
            temp.close()
            os.replace(temp.name, path)
        finally:
            if os.path.exists(temp.name):
                os.unlink(temp.name)


def state(sha):
    if not enabled() or not valid_sha(sha):
        return {"enabled": False, "state": "unavailable"}
    try:
        receipt = load_receipt(sha)
        main = ref("main")
        publication = {"main_sha": main, "is_published": contains(sha, main)}
        if not receipt:
            return {"enabled": True, "state": "not_requested", **publication}
        issue = receipt.get("issue")
        if receipt.get("decision") == "rejected":
            return {"enabled": True, "state": "rejected", "issue": issue,
                    "reason": receipt.get("reason"), "attachments": receipt.get("attachments", []), **publication}
        if publication["is_published"]:
            return {"enabled": True, "state": "published", "issue": issue, **publication}
        if type(issue) is int and issue > 0:
            current = api(f"/issues/{issue}")
            return {"enabled": True, "state": "blocked" if current.get("state") == "closed" else "queued", "issue": issue, **publication}
        return {"enabled": True, "state": "blocked", **publication}
    except (AcceptanceError, KeyError, TypeError, OSError):
        return {"enabled": True, "state": "unknown"}


def acceptance_readiness(running_sha, version, latest_version, deployments):
    """Read-only UI gate; unknown GitHub state must not enable acceptance."""
    result = {"ready": False, "reason": "正在核对候选版本与发布状态"}
    if not enabled() or not valid_sha(running_sha):
        return {**result, "reason": "当前环境未启用验收或缺少有效运行 SHA"}
    if version != latest_version:
        return {**result, "reason": "运行版本与最新版本记录不一致"}
    if not deployments or deployments[0].get("sha") != running_sha or deployments[0].get("code") not in {"install_ok", "update_ok"}:
        return {**result, "reason": "当前运行提交尚无对应的本机成功部署回执"}
    try:
        candidate = ref("preview")
    except (AcceptanceError, KeyError, TypeError, OSError):
        return {**result, "reason": "无法确认 GitHub 最新 preview，验收已锁定，请刷新重试"}
    if candidate != running_sha:
        return {**result, "reason": "preview 已更新，当前页面仍是旧版，请等待自动部署后刷新", "preview_sha": candidate}
    return {"ready": True, "reason": "", "preview_sha": candidate}


def find_issue(title, payload):
    # On a lost POST response, reuse the exact owner-created issue instead of
    # emitting a second release authorization for the same running candidate.
    for page in range(1, 6):
        items = api(f"/issues?state=all&sort=created&direction=desc&per_page=100&page={page}")
        for issue in items:
            if issue.get("title") == title and "pull_request" not in issue:
                if issue.get("author_association") != "OWNER":
                    continue
                if issue.get("body") != payload:
                    raise AcceptanceError("该候选已有不同发布基线的验收记录，请联系开发人员")
                return issue
        if len(items) < 100:
            break
    return None


def submit(sha, version, deployed):
    if not enabled():
        raise AcceptanceError("仅基座验收站允许发起正式发布")
    if not valid_sha(sha) or sha != os.getenv("APP_RUNNING_SHA") or not deployed:
        raise AcceptanceError("当前版本已变化或本机部署尚未通过，请刷新后验收")
    RECEIPTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (RECEIPTS / "request.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = load_receipt(sha)
        if existing:
            return state(sha)
        if ref("preview") != sha:
            raise AcceptanceError("preview 已有新候选，请检查更新并重新验收")
        main = ref("main")
        if contains(sha, main):
            save_receipt(sha, {"issue": None, "decision": "accepted", "at": datetime.now(timezone.utc).isoformat()})
            return {"enabled": True, "state": "published", "issue": None, "main_sha": main, "is_published": True}
        if not contains(main, sha):
            raise AcceptanceError("main 与候选存在分歧，禁止直接发布")
        evidence = test_evidence(sha)
        # Serialize the user confirmation and bind both release refs to it.
        title = f"[BASE-ACCEPTANCE {sha}]"
        payload = "[BASE_ACCEPTANCE] " + json.dumps(
            {"schema": 1, "candidate_sha": sha, "expected_main_sha": main,
             "version": version, **evidence}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        found = find_issue(title, payload)
        if ref("preview") != sha or ref("main") != main:
            raise AcceptanceError("发布基线发生变化，请刷新并重新验收")
        if not found:
            found = api("/issues", {"title": title, "body": payload})
        issue = found.get("number")
        if type(issue) is not int or issue <= 0:
            raise AcceptanceError("GitHub 未返回有效验收单号")
        save_receipt(sha, {"issue": issue, "decision": "accepted", "at": found.get("created_at") or datetime.now(timezone.utc).isoformat(), "test_head": evidence["test_head"]})
        return {"enabled": True, "state": "queued", "issue": issue}

def reject(sha, version, reason, images):
    from app import feedback_attachments
    if not enabled() or sha != os.getenv("APP_RUNNING_SHA") or not valid_sha(sha) or len(reason)<5:
        raise AcceptanceError("拒绝理由或候选SHA无效")
    RECEIPTS.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (RECEIPTS/"request.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        old=load_receipt(sha)
        if old and old.get("decision") != "rejected":
            raise AcceptanceError("已经提交验收通过，不能并发拒绝")
        if ref("preview") != sha or contains(sha,ref("main")):
            raise AcceptanceError("候选已变化或已经正式发布")
        # Reject cannot coexist with an earlier publication authorization even
        # if an earlier HTTP response or local receipt was lost.
        for page in range(1,6):
            issues=api(f"/issues?state=open&per_page=100&page={page}")
            if any(i.get("title")==f"[BASE-ACCEPTANCE {sha}]" and i.get("author_association")=="OWNER" for i in issues):
                raise AcceptanceError("当前候选已有正式发布授权，拒绝并发变更")
            if len(issues)<100:
                break
        else:
            raise AcceptanceError("无法完整核对发布授权")
        if old:
            return state(sha)
        title=f"[BASE-REJECTION {sha}]"
        existing=feedback_attachments.listing("rejection",sha)
        attachments=existing or (feedback_attachments.save("rejection",sha,images) if images else [])
        # GitHub gets the reason and authenticated evidence locator, never image bytes.
        payload="[BASE_REJECTION] "+json.dumps({"schema":1,"candidate_sha":sha,"version":version,"reason":reason,
                    "attachments":[{"id":x["id"],"url": "https://base.layho.com"+x["url"]} for x in attachments]},ensure_ascii=False,sort_keys=True)
        found=find_issue(title,payload)
        if ref("preview") != sha or contains(sha,ref("main")):
            raise AcceptanceError("候选已变化，拒绝记录未授权任何合并")
        if not found:
            found=api("/issues",{"title":title,"body":payload})
        issue=found.get("number")
        if type(issue) is not int or issue<=0:
            raise AcceptanceError("GitHub 未返回修复任务编号")
        save_receipt(sha,{"decision":"rejected","issue":issue,"reason":reason,"attachments":attachments,"at":found.get("created_at") or datetime.now(timezone.utc).isoformat()})
        return state(sha)

def local_decision(sha, head):
    """Fast local evidence only. Legacy receipts are recovered by the worker."""
    if not enabled() or not valid_sha(sha) or not valid_sha(head):
        return None
    try:
        receipt = load_receipt(sha)
        if not receipt or not isinstance(receipt.get("at"), str):
            return None
        if receipt.get("decision") == "accepted" and receipt.get("test_head") == head:
            return {"decision":"accepted","at":receipt["at"],"issue":receipt.get("issue"),"candidate_sha":sha}
        if receipt.get("decision") == "rejected":
            return {"decision":"rejected","at":receipt["at"],"issue":receipt.get("issue"),"candidate_sha":sha}
    except (AcceptanceError, OSError, ValueError, TypeError):
        pass
    return None


def collect_release_evidence(issue, comments):
    """Validate an owner-authored exact-candidate acceptance/rejection issue."""
    if issue.get("author_association") != "OWNER" or (issue.get("user") or {}).get("login") != "layho":
        return None
    title = re.fullmatch(r"\[BASE-(ACCEPTANCE|REJECTION) ([0-9a-f]{40})\]", issue.get("title") or "")
    if not title:
        return None
    kind, sha = title.groups()
    marker = "[BASE_ACCEPTANCE] " if kind == "ACCEPTANCE" else "[BASE_REJECTION] "
    body = issue.get("body") or ""
    if not body.startswith(marker):
        return None
    try:
        data = json.loads(body[len(marker):])
        at = datetime.fromisoformat(issue["created_at"].replace("Z","+00:00"))
        if at.tzinfo is None:
            return None
        if not isinstance(data,dict) or data.get("schema") != 1 or data.get("candidate_sha") != sha:
            return None
        if type(issue.get("number")) is not int:
            return None
        out={"decision":"accepted" if kind=="ACCEPTANCE" else "rejected","candidate_sha":sha,
             "at":at.isoformat(),"issue":issue["number"]}
        if kind=="REJECTION":
            return out if isinstance(data.get("reason"),str) and len(data["reason"].strip())>=5 else None
        keys={"schema","candidate_sha","expected_main_sha","version","test_head","test_base","tested_merge","tree"}
        if set(data)!=keys or not all(valid_sha(data[k]) for k in keys-{"schema","version"}):
            return None
        out["test_head"]=data["test_head"]
        # Publication has a separate trusted receipt and must match the candidate.
        for c in comments:
            if c.get("author_association")!="OWNER" or (c.get("user") or {}).get("login")!="layho":
                continue
            if c.get("updated_at")!=c.get("created_at"):
                continue
            text=c.get("body") or ""
            if not text.startswith("[BASE_PUBLISHED] "):
                continue
            event=json.loads(text[len("[BASE_PUBLISHED] "):])
            if event=={"candidate_sha":sha,"main_sha":sha,"executor":"macci-main-publish"}:
                stamp=datetime.fromisoformat(c["created_at"].replace("Z","+00:00"))
                if stamp.tzinfo and stamp>=at:
                    out["published_at"]=stamp.isoformat()
        return out
    except (ValueError,KeyError,TypeError,AttributeError):
        return None
