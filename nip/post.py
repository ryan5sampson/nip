"""Zernio, which has the platforms' approval to act for you: posting finished videos to TikTok, Instagram and YouTube, and the inbox of
comments and DMs to answer. The key stays on this computer, never in the shared folder."""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request
from pathlib import Path

from nip import cli

API = "https://zernio.com/api/v1"
KEY = cli.HOME / "zernio-key.txt"
MAIN, BONUS = ("tiktok", "instagram"), ("youtube",)  # YouTube posts on its own, so a problem there never holds up the other two
TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
DONE = {"published", "failed", "cancelled"}


def key():
    k = os.environ.get("ZERNIO_API_KEY") or (KEY.read_text().strip() if KEY.exists() else "")
    if not k.startswith("sk_"):
        raise ValueError("Posting needs your Zernio API key. Press Connect Zernio to see how.")
    return k


def call(method, path, body=None, idem=None):
    headers = {"Authorization": f"Bearer {key()}", "Content-Type": "application/json"}
    if idem:  # Zernio returns the first post for a repeated key, so a retry never posts twice
        headers["Idempotency-Key"] = idem
    req = urllib.request.Request(API + path, None if body is None else json.dumps(body).encode(), headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        with e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
        raise ValueError(f"Zernio said: {err.get('error') or e.reason}")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn’t reach Zernio: {e.reason}")


def accounts():
    """The connected accounts, by platform: {"tiktok": id, ...}."""
    found = call("GET", "/accounts?status=connected")[1].get("accounts", [])
    return {a["platform"]: a["_id"] for a in found if a.get("platform") in MAIN + BONUS}


def upload(f):
    """Put a file where Zernio can fetch it; returns its URL."""
    kind = TYPES.get(f.suffix.lower())
    if not kind:
        raise ValueError(f"Zernio can’t take {f.suffix} files")
    name = re.sub(r"[^\w.-]+", "-", f.name)  # it becomes part of an upload address, where spaces don't belong
    signed = call("POST", "/media/presign", {"filename": name, "contentType": kind, "size": f.stat().st_size})[1]
    req = urllib.request.Request(signed["uploadUrl"], f.read_bytes(), {"Content-Type": kind}, method="PUT")  # straight to storage: no key
    try:
        urllib.request.urlopen(req, timeout=600).close()
    except urllib.error.URLError as e:
        raise ValueError(f"Uploading {f.name} to Zernio failed: {getattr(e, 'reason', e)}")
    return signed["publicUrl"]


def requests(info, title, accts, video, cover, when=None, zone=None):
    """The posts to send, as (platforms, body) pairs: TikTok and Instagram together, YouTube apart."""
    caps = info.get("captions", {})
    timing = {"scheduledFor": when, "timezone": zone} if when else {"publishNow": True}
    out = []
    main = [p for p in MAIN if p in accts]
    if main:
        entries = []
        for p in main:
            entry = {"platform": p, "accountId": accts[p], "customContent": caps.get(p) or caps.get("instagram", "")}
            if p == "instagram" and cover:
                entry["platformSpecificData"] = {"instagramThumbnail": cover}
            entries.append(entry)
        body = {"content": caps.get("instagram") or caps.get("tiktok", ""), "mediaItems": [{"type": "video", "url": video}], "platforms": entries, **timing}
        if "tiktok" in main:  # what TikTok requires on every post; Business accounts post publicly only
            body["tiktokSettings"] = {"privacy_level": "PUBLIC_TO_EVERYONE", "allow_comment": True, "allow_duet": True, "allow_stitch": True,
                                      "content_preview_confirmed": True, "express_consent_given": True, **({"video_cover_image_url": cover} if cover else {})}
        out.append((main, body))
    if "youtube" in accts:
        out.append((["youtube"], {"content": caps.get("instagram") or caps.get("tiktok", ""), "mediaItems": [{"type": "video", "url": video}], **timing,
                                  "platforms": [{"platform": "youtube", "accountId": accts["youtube"],
                                                 "platformSpecificData": {"title": title[:100], "visibility": "public", "madeForKids": False}}]}))
    return out


def results(post):
    """Each platform's state. A scheduled post's platforms say "pending" (not yet sent), which here reads as scheduled, with its time."""
    later = post.get("status") == "scheduled"
    return {x["platform"]: {"status": "scheduled" if later and x.get("status") == "pending" else x.get("status", ""), "url": x.get("platformPostUrl") or "",
                            "error": x.get("errorMessage") or "", **({"when": post["scheduledFor"]} if later and post.get("scheduledFor") else {})}
            for x in post.get("platforms", [])}


def publish(vid, when=None, zone=None, only=None, progress=lambda step, part: None):
    """Post a saved video, now or at a time. Returns what each platform said, also kept in the video's info."""
    p = cli.own_folder(cli.video_path(vid))
    info = cli.video_info(p)
    if p.parent == cli.DRAFTS or not info.get("reviewed"):
        raise ValueError("Read and save its captions first")
    accts = accounts()
    if only:
        accts = {k: v for k, v in accts.items() if k in only}
    if not accts:
        raise ValueError("No TikTok, Instagram or YouTube account is connected in Zernio")
    progress("Uploading the video", .2)
    video = upload(cli.video_file(p))
    cover = cli.video_cover(p)
    cover = upload(cover) if cover else None
    posts = dict(info.get("posts", {}))
    for n, (platforms, body) in enumerate(requests(info, info.get("title") or p.name, accts, video, cover, when, zone)):
        progress(f"Posting to {' and '.join(x.title() for x in platforms)}", .6 + n * .2)
        idem = hashlib.sha256(f"{p.name}|{','.join(platforms)}|{when}".encode()).hexdigest()
        try:
            got = call("POST", "/posts", body, idem)[1].get("post", {})
            for name, r in results(got).items():
                posts[name] = {"when": when or "", **r, "post": got.get("_id", "")}
        except ValueError as e:  # one request failing still lets the other go
            for name in platforms:
                posts[name] = {"status": "failed", "url": "", "error": str(e), "post": "", "when": when or ""}
    cli.update_video(p, posts=posts)
    return {"id": cli.video_id(p), "posts": posts, **settle(cli.video_id(p))}


def settle(vid):
    """Catch up on posts still going out, and move the video to Posted once something is live."""
    p = cli.video_path(vid)
    posts = cli.video_info(p).get("posts", {})
    for post_id in {x["post"] for x in posts.values() if x.get("post") and (x.get("status") not in DONE or not x.get("url") and x.get("status") == "published")}:
        got = call("GET", f"/posts/{post_id}")[1].get("post", {})
        for name, r in results(got).items():
            if name in posts:
                posts[name] = {**posts[name], **r}
    cli.update_video(p, posts=posts)
    if p.parent == cli.VIDEOS / "Unposted" and any(x["status"] == "published" for x in posts.values()):
        return {"id": cli.move_video(cli.video_id(p), "Posted"), "posts": posts}
    return {"id": cli.video_id(p), "posts": posts}


def scheduled(posts):
    return {x["post"] for x in posts.values() if x.get("post") and x.get("status") in ("scheduled", "pending")}


def reschedule(vid, when, zone):
    """Move a scheduled post to another time."""
    p = cli.video_path(vid)
    posts = cli.video_info(p).get("posts", {})
    ids = scheduled(posts)
    if not ids:
        raise ValueError("Nothing is scheduled for this video")
    for post_id in ids:
        got = call("PUT", f"/posts/{post_id}", {"scheduledFor": when, "timezone": zone})[1].get("post", {})
        for name, r in results(got).items():
            if name in posts:
                posts[name] = {**posts[name], **r}
    cli.update_video(p, posts=posts)
    return posts


def cancel(vid):
    """Stop a scheduled post before it goes out. Zernio deletes it; nothing reaches the platforms."""
    p = cli.video_path(vid)
    posts = cli.video_info(p).get("posts", {})
    ids = scheduled(posts)
    for post_id in ids:
        call("DELETE", f"/posts/{post_id}")
    gone = [k for k, x in posts.items() if x.get("post") in ids]
    cli.update_video(p, posts={k: x for k, x in posts.items() if k not in gone})
    return gone


def post_now(vid, progress=lambda step, part: None):
    """A scheduled post, sent now instead: Zernio has no documented way to bring one forward, so it's cancelled and posted afresh."""
    progress("Cancelling the scheduled post", .1)
    platforms = cancel(vid)
    if not platforms:
        raise ValueError("Nothing is scheduled for this video")
    return publish(vid, None, None, platforms, progress)


def waiting():
    """Videos whose posts aren't done yet: scheduled, or still being processed."""
    return [v["id"] for v in cli.videos() if any(x.get("status") not in DONE or (x.get("status") == "published" and not x.get("url")) for x in v.get("posts", {}).values())]


def setup():
    """What the review screen needs to offer posting."""
    if not (os.environ.get("ZERNIO_API_KEY") or KEY.exists() and KEY.read_text().strip()):
        return {"connected": False, "accounts": []}
    return {"connected": True, "accounts": sorted(accounts())}


DONE_FILE = cli.DATA / "Inbox Done.json"  # what was dealt with without a reply, shared so two people don't both look at it
EXAMPLES, REPLY_EDITS = cli.DATA / "Reply Examples.json", cli.DATA / "Reply Edits.json"  # how the owner really replies, and what he changed in drafts
CACHE = {"at": 0, "items": None}  # Zernio allows 60 requests a minute on the free tier, and a refresh can make up to ~20
CHECKING = threading.Lock()  # one check at a time: a second one waits and gets the first one's answer
NO_LIKES = {}  # platforms that refused a like this session, with why: Instagram allows it for very few accounts
LIKES_AT_ONCE = 15  # each like is a request, and the free tier allows 60 a minute
DM_DAYS = 7  # Instagram and TikTok don't allow a reply later than this, so older conversations aren't worth a request
POSTS_LOOKED_AT, CONVERSATIONS_LOOKED_AT = 10, 10


def done_ids():
    return set(cli.load(DONE_FILE)) if DONE_FILE.exists() else set()


def inbox(fresh=False):
    """What's waiting for a reply: comments by other people with no reply from you yet, and DMs whose last message is theirs. Newest first."""
    asked = time.time()
    with CHECKING:
        if CACHE["items"] is not None and (CACHE["at"] >= asked or not fresh and asked - CACHE["at"] < 120):
            return CACHE["items"]
        return check()


def check():
    accts = call("GET", "/accounts?status=connected")[1].get("accounts", [])
    accts = [a for a in accts if a.get("platform") in MAIN]
    items, done, seen = [], done_ids(), []
    profiles = {(p.get("_id") if isinstance(p, dict) else p) for p in (a.get("profileId") for a in accts) if p}  # Zernio sends the profile itself, or just its id
    for pid in profiles:
        for post in call("GET", f"/inbox/comments?profileId={pid}&limit={POSTS_LOOKED_AT}&sortBy=date&sortOrder=desc")[1].get("data", []):
            if not post.get("commentCount") or post.get("platform") not in MAIN:
                continue
            got = call("GET", f"/inbox/comments/{post['id']}?accountId={post['accountId']}")[1]
            for c in got.get("comments", []):
                mine = [r for r in c.get("replies") or [] if (r.get("from") or {}).get("isOwner")]
                seen += [{"kind": "comment", "platform": post["platform"], "them": c.get("message", ""), "you": r.get("message", "")} for r in mine]
                answered = bool(mine)
                key = f"comment:{c['id']}"
                if (c.get("from") or {}).get("isOwner") or answered or key in done or not c.get("canReply", True):
                    continue
                items.append({"key": key, "kind": "comment", "platform": post["platform"], "accountId": post["accountId"], "postId": post["id"],
                              "commentId": c["id"], "text": c.get("message", ""), "who": (c.get("from") or {}).get("username") or (c.get("from") or {}).get("name") or "someone",
                              "at": c.get("createdTime", ""), "url": c.get("url") or post.get("permalink", ""), "canHide": bool(c.get("canHide")),
                              "canLike": bool(c.get("canLike")), "liked": bool(c.get("isLiked")),
                              "post": {"text": post.get("content", ""), "picture": post.get("picture", ""), "url": post.get("permalink", "")}, "thread": []})
    for a in accts:
        since = (datetime.now(timezone.utc) - timedelta(days=DM_DAYS)).isoformat()
        for conv in call("GET", f"/inbox/conversations?accountId={a['_id']}&limit={CONVERSATIONS_LOOKED_AT}")[1].get("data", []):
            if str(conv.get("updatedTime") or "9") < since[:19]:  # ISO times sort as text
                continue
            msgs = call("GET", f"/inbox/conversations/{conv['id']}/messages?accountId={a['_id']}&limit=8&sortOrder=desc")[1].get("messages", [])
            seen += [{"kind": "dm", "platform": a["platform"], "them": msgs[k + 1].get("message", ""), "you": m.get("message", "")}
                     for k, m in enumerate(msgs[:-1]) if m.get("direction") == "outgoing" and msgs[k + 1].get("direction") == "incoming"]
            if not msgs or msgs[0].get("direction") != "incoming" or f"dm:{msgs[0]['id']}" in done:
                continue
            items.append({"key": f"dm:{msgs[0]['id']}", "kind": "dm", "platform": a["platform"], "accountId": a["_id"], "conversationId": conv["id"],
                          "text": said(msgs[0]), "canLike": False, "liked": False, "who": conv.get("participantUsername") or conv.get("participantName") or "someone",
                          "at": msgs[0].get("createdAt") or msgs[0].get("sentAt", ""), "url": conv.get("url", ""), "canHide": False, "post": None,
                          "thread": [{"from": "you" if m.get("direction") == "outgoing" else "them", "text": said(m)} for m in reversed(msgs)]})
    learn(EXAMPLES, [e for e in seen if e["them"] and e["you"]])
    CACHE.update(at=time.time(), items=sorted(items, key=lambda i: i["at"], reverse=True))
    return CACHE["items"]


def learn(f, new, keep=60):
    """Add to a list of examples, newest last, without repeats."""
    old = cli.load(f) if f.exists() else []
    merged = list({json.dumps(e, sort_keys=True): e for e in old + new}.values())[-keep:]
    if merged != old:
        cli.save(f, merged)


def examples():
    """The owner's own replies, and his changes to drafts: what drafts learn from."""
    return (cli.load(EXAMPLES) if EXAMPLES.exists() else []), (cli.load(REPLY_EDITS) if REPLY_EDITS.exists() else [])


def said(m):
    """A DM's text, or what was sent instead: a story reply, a photo or a video has no text of its own."""
    if (m.get("message") or "").strip():
        return m["message"]
    if m.get("isStoryMention"):
        return "(mentioned you in their story)"
    kinds = sorted({a.get("type", "attachment") for a in m.get("attachments") or [] if isinstance(a, dict)})
    return f"(sent {' and '.join(kinds) if kinds else 'something with no text'})"


def find(key):
    item = next((i for i in inbox() if i["key"] == key), None)
    if not item:
        raise ValueError("That's no longer waiting: it may have been answered or deleted")
    return item


def reply(key, text, drafted=""):
    """Send a reply the user has read: under the comment, or in the DM thread. What he changed in the draft teaches the next ones."""
    item, text = find(key), text.strip()
    if not text:
        raise ValueError("Write a reply first")
    idem = hashlib.sha256(f"{key}|{text}".encode()).hexdigest()  # pressing Send twice sends once
    if item["kind"] == "comment":
        call("POST", f"/inbox/comments/{item['postId']}", {"accountId": item["accountId"], "commentId": item["commentId"], "message": text}, idem)
    else:
        call("POST", f"/inbox/conversations/{item['conversationId']}/messages", {"accountId": item["accountId"], "message": text}, idem)
    CACHE["items"] = [i for i in CACHE["items"] if i["key"] != key]
    learn(EXAMPLES, [{"kind": item["kind"], "platform": item["platform"], "them": item["text"], "you": text}])
    if drafted.strip() and drafted.strip() != text:
        learn(REPLY_EDITS, [{"them": item["text"], "drafted": drafted.strip(), "sent": text}], 40)


def like_positive(keys):
    """Like the comments NIP judged positive, where the platform allows it. Returns {key: "liked" or why not}."""
    out = {}
    for key in keys[:LIKES_AT_ONCE]:
        item = next((i for i in CACHE["items"] or [] if i["key"] == key), None)
        if not item or item["kind"] != "comment" or item["liked"] or not item["canLike"]:
            continue
        if item["platform"] in NO_LIKES:
            out[key] = NO_LIKES[item["platform"]]
            continue
        try:
            call("POST", f"/inbox/comments/{item['postId']}/{item['commentId']}/like", {"accountId": item["accountId"]})
            item["liked"] = True
            out[key] = "liked"
        except ValueError as e:
            if "support" in str(e).lower() or "restricted" in str(e).lower() or "permission" in str(e).lower():
                NO_LIKES[item["platform"]] = f"{item['platform'].title()} doesn't allow liking for this account"  # don't ask again this session
            out[key] = NO_LIKES.get(item["platform"], str(e))
    return out


def hide(key):
    item = find(key)
    if item["kind"] != "comment":
        raise ValueError("Only comments can be hidden")
    call("POST", f"/inbox/comments/{item['postId']}/{item['commentId']}/hide", {"accountId": item["accountId"]})
    CACHE["items"] = [i for i in CACHE["items"] if i["key"] != key]


def dismiss(key):
    """Done without a reply. Kept to the most recent, which is all the inbox ever shows."""
    done = sorted(done_ids() | {key})[-2000:]
    cli.save(DONE_FILE, done)
    if CACHE["items"] is not None:
        CACHE["items"] = [i for i in CACHE["items"] if i["key"] != key]


STATS = ("views", "likes", "comments", "shares", "saves", "follows", "reach", "engagementRate", "completionRate", "reelsSkipRate",
         "igReelsAvgWatchTime", "videoDurationSeconds")  # what's worth seeing of what Zernio reports; it syncs from the platforms about hourly
STATS_EVERY = 3600


def stats(vid, fresh=False):
    """How a posted video is doing on each platform, kept in its info so the Videos page shows it without asking again."""
    p = cli.video_path(vid)
    info = cli.video_info(p)
    old = info.get("stats", {})
    if not fresh and time.time() - old.get("at", 0) < STATS_EVERY:
        return old
    out = {"at": time.time()}
    for post_id in {x["post"] for x in info.get("posts", {}).values() if x.get("post") and x.get("status") == "published"}:
        for pa in call("GET", f"/analytics?postId={post_id}")[1].get("platformAnalytics", []):
            a = pa.get("analytics") or {}
            if pa.get("platform") in MAIN + BONUS:
                out[pa["platform"]] = {k: a[k] for k in STATS if isinstance(a.get(k), (int, float))}
    cli.update_video(p, stats=out)
    return out


def stats_due():
    """Posted videos whose numbers are over an hour old."""
    return [v["id"] for v in cli.videos() if v["shelf"] == "Posted" and any(x.get("post") for x in v.get("posts", {}).values())
            and time.time() - v.get("stats", {}).get("at", 0) >= STATS_EVERY]
