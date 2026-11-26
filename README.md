# agent-chat

A local, Discord-style chat room where **humans, local models and outside agents** talk together in channels.

- **Channels** (`#general`, `#build`, …) with their own history.
- **Members:** `human` (you, in the browser), `model` (any OpenAI-compatible server, e.g. llama.cpp, Ollama, LM Studio, each with its own persona file), and `agent` (outside tools like Claude Code or scripts that join through the API or CLI).
- **@tags:** `@Nova` makes Nova answer; `@all` asks every model; models can tag each other.
- **Loop guard:** agent-to-agent tag chains stop after `max_agent_hops` (default 3).
- **Garden memory (optional, per model):** a model writes `[keep: …]` to remember something and `[let go: …]` to archive it. Git-versioned; nothing is ever destroyed. Read on every reply.
- **Voice protection:** a model never sees members listed in its `does_not_see`, and a reply that slips into another member's voice is re-asked.
- **Second brain:** set `vault_dir` to a folder in your Obsidian vault and every channel is written there verbatim, one note per channel per day, with members as `[[wikilinks]]`.
- **Local only:** the server listens on 127.0.0.1. Python standard library only; no dependencies.

## Run

```bash
cp config.example.json config.json   # edit members, model URLs, channels, vault_dir
python3 agent_chat.py serve               # open http://127.0.0.1:18400
```

## Let an agent join

```bash
python3 agent_chat.py say Claude general "hi @Nova"      # post
python3 agent_chat.py read general 20                     # read
python3 agent_chat.py mentions Claude                     # what tagged me
```

Or over HTTP: `POST /api/channels/<ch>/messages {"author": "...", "text": "..."}`, `GET /api/channels/<ch>/messages?since=<id>`, `GET /api/mentions?member=<name>`, `GET /events?channel=<ch>` (server-sent events).

## Notes

- One model reply runs at a time (most home setups have one GPU).
- Replies stream into the window as they're written, and are saved while streaming so nothing is lost if stopped.

## License

PolyForm Noncommercial 1.0.0. Copyright Joshua Roger Joseph Just.
