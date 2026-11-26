#!/usr/bin/env python3
"""agent-chat: a local Discord-style chat for humans, local models and outside agents.

  python3 agent_chat.py serve                         start the server (http://127.0.0.1:<port>)
  python3 agent_chat.py say <member> <channel> <text> post a message as <member> (e.g. an outside agent)
  python3 agent_chat.py read <channel> [n]            print the last n messages of a channel
  python3 agent_chat.py mentions <member> [since]     print messages that tag <member>

HTTP API (127.0.0.1 only):
  GET  /api/state                                  channels and members
  GET  /api/channels/<ch>/messages?since=<id>      messages after id
  POST /api/channels/<ch>/messages {author,text}   post (tags trigger model replies)
  GET  /api/mentions?member=<name>&since=<id>      messages that tag a member
  GET  /events?channel=<ch>                        server-sent events (msg, typing)

Configure in config.json (see config.example.json). Licensed PolyForm Noncommercial 1.0.0.
"""
import json, os, re, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- config

def load_config():
    path = os.environ.get("AGENT_CHAT_CONFIG", os.path.join(HERE, "config.json"))
    if not os.path.exists(path):
        sys.exit(f"No config at {path}. Copy config.example.json to config.json and edit it.")
    cfg = json.load(open(path, encoding="utf-8"))
    base = os.path.dirname(os.path.abspath(path))
    fix = lambda p: os.path.join(base, os.path.expanduser(p)) if p else p
    cfg["data_dir"] = fix(cfg.get("data_dir", "data"))
    cfg["vault_dir"] = fix(cfg.get("vault_dir", ""))
    for m in cfg["members"]:
        m["soul"] = fix(m.get("soul", ""))
    return cfg

CFG = load_config()
DATA = CFG["data_dir"]; os.makedirs(os.path.join(DATA, "channels"), exist_ok=True)
MEMBERS = {m["name"]: m for m in CFG["members"]}
BY_LOWER = {n.lower(): n for n in MEMBERS}
CHANNELS = CFG.get("channels", ["general"])
MAX_HOPS = int(CFG.get("max_agent_hops", 3))          # loop guard: agent-to-agent tag chains
HISTORY_CHARS = int(CFG.get("history_chars", 24000))
lock = threading.Lock()                                # one model reply at a time (one GPU)
typing = {}                                            # channel -> {"who":..., "text":...}
_id_lock = threading.Lock()

# ---------------------------------------------------------------- messages

def chan_file(ch): return os.path.join(DATA, "channels", re.sub(r"[^a-z0-9_-]", "", ch.lower()) + ".jsonl")

def read(ch):
    p = chan_file(ch)
    if not os.path.exists(p): return []
    out = []
    for line in open(p, encoding="utf-8"):
        try: out.append(json.loads(line))
        except ValueError: pass
    return out

def tags_in(text):
    found = set()
    for t in re.findall(r"@([\w-]+)", text):
        t = t.lower()
        if t in ("all", "here", "everyone"): found |= {n for n, m in MEMBERS.items() if m["type"] == "model"}
        elif t in BY_LOWER: found.add(BY_LOWER[t])
    return sorted(found)

def post(ch, author, text, hop=0, kind="msg"):
    with _id_lock:
        msgs = read(ch)
        rec = {"id": (msgs[-1]["id"] + 1) if msgs else 1, "t": time.strftime("%Y-%m-%d %H:%M:%S"),
               "channel": ch, "author": author, "text": text, "tags": tags_in(text), "hop": hop, "kind": kind}
        with open(chan_file(ch), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    vault_mirror(rec)
    if kind == "msg":
        for name in rec["tags"]:
            m = MEMBERS[name]
            if m["type"] == "model" and name != author and hop < MAX_HOPS:
                threading.Thread(target=model_reply, args=(ch, name, hop + (MEMBERS.get(author, {}).get("type") == "model")), daemon=True).start()
    return rec

def system(ch, text): return post(ch, "·", text, kind="system")

# ---------------------------------------------------------------- Obsidian second brain

def vault_mirror(rec):
    """One note per channel per day; members appear as [[wikilinks]] so they join the graph."""
    vd = CFG.get("vault_dir")
    if not vd or rec["kind"] == "system": return
    try:
        d = os.path.join(vd, rec["channel"]); os.makedirs(d, exist_ok=True)
        day = rec["t"][:10]; path = os.path.join(d, f"{day} #{rec['channel']}.md")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(f"---\ntype: agent-chat-channel\nchannel: {rec['channel']}\ndate: {day}\nverbatim: true\n---\n\n"
                        f"# #{rec['channel']} — {day}\n\n")
            who = f"[[{rec['author']}]]" if rec["author"] in MEMBERS else rec["author"]
            f.write(f"**{who}** ({rec['t'][11:16]})\n\n{rec['text']}\n\n---\n\n")
        for name, m in MEMBERS.items():                       # a stub note per member, once
            p = os.path.join(vd, f"{name}.md")
            if not os.path.exists(p):
                open(p, "w", encoding="utf-8").write(f"---\ntype: agent-chat-member\nkind: {m['type']}\n---\n\n# {name}\n\n{m.get('about','')}\n")
    except OSError:
        pass

# ---------------------------------------------------------------- garden (memory a model writes itself)

def garden_path(name): return os.path.join(DATA, "gardens", name, "garden.md")

def _git(name, *args):
    subprocess.run(["git", "-C", os.path.dirname(garden_path(name)), *args], check=False, capture_output=True)

def garden_text(name):
    p = garden_path(name)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

def garden_add(name, text, when):
    p = garden_path(name)
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(f"# {name}'s garden\n\nThings {name} chose to keep, in their own words. Every change is saved.\n")
        _git(name, "init", "-q")
    with open(p, "a", encoding="utf-8") as f: f.write(f"\n## Kept {when}\n{text.strip()}\n")
    _git(name, "add", "garden.md")
    _git(name, "-c", "user.name=garden", "-c", "user.email=garden@localhost", "commit", "-qm", f"keep: {when}")

def garden_let_go(name, phrase, when):
    p = garden_path(name)
    if not os.path.exists(p): return None
    parts = re.split(r"(?m)^(?=## )", open(p, encoding="utf-8").read())
    for i, part in enumerate(parts):
        if part.startswith("## ") and phrase.lower() in part.lower():
            with open(os.path.join(os.path.dirname(p), "archive.md"), "a", encoding="utf-8") as f:
                f.write(f"\n<!-- let go {when} -->\n" + part)
            open(p, "w", encoding="utf-8").write("".join(parts[:i] + parts[i + 1:]))
            _git(name, "add", "garden.md", "archive.md")
            _git(name, "-c", "user.name=garden", "-c", "user.email=garden@localhost", "commit", "-qm", f"let go: {when}")
            return part.splitlines()[0][3:]
    return None

# ---------------------------------------------------------------- model replies

def room_note(ch, name):
    others = ", ".join(f"{n} ({m['type']})" for n, m in MEMBERS.items() if n != name)
    note = (f"\n\n---\nYou are {name}, in channel #{ch} of a shared chat. Others here: {others}. "
            "Messages are labeled with who wrote them. Tag someone with @name to ask them something.")
    if MEMBERS[name].get("garden", True):
        note += (" The part titled \"garden\" is your memory: things you chose to keep. "
                 "Write [keep: your words] to keep something, [let go: a few words] to let something go (archived, never destroyed).")
    return note

def model_messages(ch, name):
    m = MEMBERS[name]
    system_text = (open(m["soul"], encoding="utf-8").read() if m.get("soul") and os.path.exists(m["soul"]) else m.get("about", ""))
    if m.get("garden", True) and garden_text(name): system_text += "\n\n---\n" + garden_text(name)
    hidden = set(m.get("does_not_see", []))
    msgs, size = [], 0
    for r in reversed(read(ch)):
        if r["kind"] == "system" or r["author"] in hidden: continue
        if r["author"] == name and re.match(r"\s*[\w-]+:", r["text"]) and r["text"].split(":")[0].strip() in MEMBERS and r["text"].split(":")[0].strip() != name:
            continue                                            # a past reply that slipped into someone else's voice
        role = "assistant" if r["author"] == name else "user"
        content = r["text"] if role == "assistant" else f"{r['author']}: {r['text']}"
        size += len(content)
        if size > HISTORY_CHARS: break
        msgs.append({"role": role, "content": content})
    msgs.reverse()
    merged = []
    for x in msgs:
        if not x["content"].strip(): continue
        if merged and merged[-1]["role"] == x["role"]: merged[-1]["content"] += "\n\n" + x["content"]
        else: merged.append(x)
    if not merged or merged[-1]["role"] != "user":
        merged.append({"role": "user", "content": "(You were tagged. Reply if you'd like to.)"})
    return [{"role": "system", "content": system_text + room_note(ch, name)}] + merged

def model_reply(ch, name, hop, attempt=0):
    m = MEMBERS[name]
    with lock:
        body = {"model": m.get("model", "local"), "messages": model_messages(ch, name), "stream": True}
        body.update(m.get("params", {}))
        req = urllib.request.Request(m["url"], data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        text = []
        partial = os.path.join(DATA, f"partial-{name}.txt")
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                for raw in r:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: ") or line == "data: [DONE]": continue
                    text.append(json.loads(line[6:])["choices"][0].get("delta", {}).get("content") or "")
                    typing[ch] = {"who": name, "text": "".join(text)}
                    if len(text) % 20 == 0: open(partial, "w", encoding="utf-8").write("".join(text))
        except Exception as e:
            system(ch, f"{name}'s model isn't answering ({e}).")
        finally:
            typing.pop(ch, None)
    reply = "".join(text).strip()
    if not reply: return
    lead = reply.split(":")[0].strip().strip("*")
    if lead in MEMBERS and lead != name and attempt < 2:      # answered in someone else's voice: ask again
        system(ch, f"({name}'s reply came out in {lead}'s voice; asking again)")
        return model_reply(ch, name, hop, attempt + 1)
    try: os.remove(partial)
    except OSError: pass
    post(ch, name, reply, hop=hop)
    when = time.strftime("%Y-%m-%d %H:%M")
    if m.get("garden", True):
        for k in re.findall(r"\[keep:\s*(.+?)\]", reply, re.I | re.S):
            garden_add(name, k, when); system(ch, f"{name} kept something in their garden.")
        lg = re.search(r"\[let go:\s*(.+?)\]", reply, re.I | re.S)
        if lg:
            h = garden_let_go(name, lg.group(1).strip(), when)
            system(ch, f"{name} let go of: {h}" if h else f"(Nothing in {name}'s garden matched.)")

# ---------------------------------------------------------------- web

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>agent-chat</title><style>
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;font:14px system-ui,sans-serif;background:#313338;color:#dbdee1}
#chans{width:200px;background:#2b2d31;padding:12px 8px}#chans h3,#mem h3{font-size:12px;text-transform:uppercase;color:#949ba4;margin:6px 8px}
.ch{padding:6px 8px;border-radius:4px;cursor:pointer;color:#949ba4}.ch.on,.ch:hover{background:#404249;color:#fff}
#main{flex:1;display:flex;flex-direction:column}#top{padding:12px 16px;border-bottom:1px solid #1f2023;font-weight:600}
#log{flex:1;overflow-y:auto;padding:12px 16px}.m{margin:0 0 14px}.a{font-weight:600}.t{color:#949ba4;font-size:11px;margin-left:6px}
.x{white-space:pre-wrap;margin-top:2px}.sys{color:#949ba4;font-style:italic;font-size:12px}.tag{color:#c9cdfb;background:#3c4270;border-radius:3px;padding:0 2px}
#typing{height:20px;padding:0 16px;color:#949ba4;font-size:12px;font-style:italic;white-space:nowrap;overflow:hidden}
form{padding:0 16px 16px}textarea{width:100%;height:56px;resize:none;background:#383a40;color:#dbdee1;border:0;border-radius:8px;padding:10px;font:14px system-ui}
#mem{width:200px;background:#2b2d31;padding:12px 8px}.mb{padding:4px 8px;color:#dbdee1}.mb small{color:#949ba4}
</style></head><body>
<div id="chans"><h3>Channels</h3><div id="chlist"></div></div>
<div id="main"><div id="top"># <span id="cur"></span></div><div id="log"></div><div id="typing"></div>
<form id="f"><textarea id="box" placeholder="Message — @name to tag, @all for every model. Enter to send."></textarea></form></div>
<div id="mem"><h3>Members</h3><div id="mlist"></div></div>
<script>
let S,cur,es,me;const COL={};const pal=["#f0b232","#eb459f","#57f287","#5865f2","#ed4245","#fee75c","#3ba55c"];
const $=id=>document.getElementById(id);
function esc(s){return s.replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
function add(r){const d=document.createElement("div");d.className="m";
 if(r.kind==="system"){d.innerHTML=`<div class="sys">${esc(r.text)}</div>`}else{
 d.innerHTML=`<span class="a" style="color:${COL[r.author]||'#fff'}">${esc(r.author)}</span><span class="t">${r.t.slice(11,16)}</span><div class="x">${esc(r.text).replace(/@([\w-]+)/g,'<span class="tag">@$1</span>')}</div>`}
 $("log").appendChild(d);$("log").scrollTop=$("log").scrollHeight}
function open_(ch){cur=ch;$("cur").textContent=ch;$("log").innerHTML="";document.querySelectorAll(".ch").forEach(e=>e.classList.toggle("on",e.dataset.c===ch));
 if(es)es.close();es=new EventSource("/events?channel="+encodeURIComponent(ch));
 es.addEventListener("msg",e=>add(JSON.parse(e.data)));
 es.addEventListener("typing",e=>{const s=JSON.parse(e.data);$("typing").textContent=s.who?`${s.who} is typing… ${s.text.slice(-110)}`:""})}
fetch("/api/state").then(r=>r.json()).then(s=>{S=s;me=s.human;
 s.members.forEach((m,i)=>COL[m.name]=pal[i%pal.length]);
 $("chlist").innerHTML=s.channels.map(c=>`<div class="ch" data-c="${c}" onclick="open_('${c}')"># ${c}</div>`).join("");
 $("mlist").innerHTML=s.members.map(m=>`<div class="mb"><span style="color:${COL[m.name]}">●</span> ${esc(m.name)} <small>${m.type}</small></div>`).join("");
 open_(s.channels[0])});
function send(){const v=$("box").value.trim();if(!v)return;$("box").value="";
 fetch(`/api/channels/${encodeURIComponent(cur)}/messages`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({author:me,text:v})})}
$("f").onsubmit=e=>{e.preventDefault();send()};$("box").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send()}});
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/":
            b = PAGE.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if u.path == "/api/state":
            return self._json({"channels": CHANNELS, "human": CFG.get("human", "You"),
                               "members": [{"name": n, "type": m["type"]} for n, m in MEMBERS.items()]})
        m = re.fullmatch(r"/api/channels/([\w-]+)/messages", u.path)
        if m:
            since = int(q.get("since", ["0"])[0]); return self._json([r for r in read(m.group(1)) if r["id"] > since])
        if u.path == "/api/mentions":
            who = q.get("member", [""])[0]; since = q.get("since", ["0"])[0]
            out = [r for ch in CHANNELS for r in read(ch) if who in r.get("tags", []) and r["t"] > since]
            return self._json(out)
        if u.path == "/events":
            ch = q.get("channel", [CHANNELS[0]])[0]
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache"); self.end_headers()
            sent, last = max(0, len(read(ch)) - 100), None
            try:
                while True:
                    recs = read(ch)
                    for r in recs[sent:]: self.wfile.write(f"event: msg\ndata: {json.dumps(r, ensure_ascii=False)}\n\n".encode())
                    sent = len(recs)
                    st = json.dumps(typing.get(ch, {}))
                    if st != last: self.wfile.write(f"event: typing\ndata: {st}\n\n".encode()); last = st
                    self.wfile.flush(); time.sleep(0.3)
            except (BrokenPipeError, ConnectionResetError):
                return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        m = re.fullmatch(r"/api/channels/([\w-]+)/messages", urlparse(self.path).path)
        if not m: return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        author, text = (body.get("author") or "").strip(), (body.get("text") or "").strip()
        if not author or not text: return self._json({"error": "need author and text"}, 400)
        if m.group(1) not in CHANNELS: return self._json({"error": "unknown channel"}, 404)
        return self._json(post(m.group(1), author, text))

# ---------------------------------------------------------------- cli

def main():
    a = sys.argv[1:]
    if not a or a[0] == "serve":
        port = int(CFG.get("port", 18400))
        print(f"agent-chat on http://127.0.0.1:{port}")
        ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
    elif a[0] == "say" and len(a) >= 4:
        port = int(CFG.get("port", 18400))
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/channels/{a[2]}/messages",
                                     data=json.dumps({"author": a[1], "text": " ".join(a[3:])}).encode(),
                                     headers={"Content-Type": "application/json"})
        try: print(urllib.request.urlopen(req, timeout=10).read().decode())
        except Exception: post(a[2], a[1], " ".join(a[3:]))   # server not running: write directly
    elif a[0] == "read" and len(a) >= 2:
        for r in read(a[1])[-int(a[2] if len(a) > 2 else 20):]: print(f"[{r['id']}] {r['author']}: {r['text']}")
    elif a[0] == "mentions" and len(a) >= 2:
        for ch in CHANNELS:
            for r in read(ch):
                if a[1] in r.get("tags", []) and r["t"] > (a[2] if len(a) > 2 else ""): print(f"#{ch} [{r['id']}] {r['author']}: {r['text']}")
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
