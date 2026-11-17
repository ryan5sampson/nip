"""Posting finished videos to TikTok, Instagram and YouTube through Zernio, which has the platforms' approval to post for you.
The key stays on this computer, never in the shared folder."""
import hashlib
import json
import os
import re
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
    return {x["platform"]: {"status": x.get("status", ""), "url": x.get("platformPostUrl") or "", "error": x.get("errorMessage") or ""}
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
                posts[name] = {**r, "post": got.get("_id", ""), "when": when or ""}
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


def waiting():
    """Videos whose posts aren't done yet: scheduled, or still being processed."""
    return [v["id"] for v in cli.videos() if any(x.get("status") not in DONE or (x.get("status") == "published" and not x.get("url")) for x in v.get("posts", {}).values())]


def setup():
    """What the review screen needs to offer posting."""
    if not (os.environ.get("ZERNIO_API_KEY") or KEY.exists() and KEY.read_text().strip()):
        return {"connected": False, "accounts": []}
    return {"connected": True, "accounts": sorted(accounts())}
