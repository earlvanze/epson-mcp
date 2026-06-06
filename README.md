# Epson WF-2250 MCP Server

A Model-Context-Protocol server for the **Epson WF-2250** (and any other
network-attached Epson inkjet that speaks ESC/P-R over TCP/9100).  Exposes
print, status, jobs and ink tools to any OpenClaw container or agentic
harness on the Tailnet.

## What works

- Print plain text via ESC/P-R (the `epson_print_text` tool renders a tiny
  ESC/P-R document and ships it to the printer).
- Print arbitrary base64-encoded payload (`epson_print_raw`).
- Print a file from the bind-mounted share (`epson_print_file`).
- Diagnose connectivity and capabilities (`epson_diag`, `epson_status`).
- List / cancel Windows spooler jobs (mount the helper scripts and set
  `EPSON_MCP_WIN_SCRIPT_DIR`).
- Bearer-token authenticated HTTP transport over Tailnet.

## What does NOT work (and won't)

- Network scan / network copy: the WF-2250 does not speak scan protocols
  over the network.  Use a USB-attached host or the device front panel.
- Ink levels: not exposed via PJL/ESC/P on this model.
- LPD fallback: the server is started, but most low-end Epson inkjets
  ignore the LPD port (515 closed in our probe).

## Architecture

- Pure Python 3.12 stdlib (`BaseHTTPRequestHandler` + `ThreadingHTTPServer`).
- JSON-RPC 2.0 protocol (compatible with MCP) implemented manually so the
  image stays small and the transport is portable.
- Two transports: stdio (for direct openclaw integration) and HTTP (for
  Tailnet access with bearer auth).
- Backends: `raw9100` (TCP/9100 ESC/P-R), `lpd` (TCP/515 LPR), `windows`
  (PowerShell helpers in mounted dir).
- Reliable connection: the container reaches the printer via a Windows-side
  `netsh interface portproxy` (`0.0.0.0:19100 → 192.168.4.21:9100`).  Inside
  the container, `host.docker.internal:19100` resolves to the Windows host
  through Docker Desktop's host-gateway.  `_raw_send` retries 5 times with
  exponential backoff (0.5s base) to handle WSL2/Docker NAT flakiness.

## Run on Cyber (primary)

1.  Set up the Windows-side portproxy (one-time, persists):

    ```powershell
    netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=19100 connectaddress=192.168.4.21 connectport=9100
    ```

2.  Build and run the container:

    ```bash
    cd G:\CyberUmbrel\umbrel-docker\data\app-data\epson-mcp
    docker build -t epson-mcp:local .
    docker compose up -d
    ```

3.  The container listens on `http://127.0.0.1:18790`.  Healthz path:
    `/healthz`.  JSON-RPC path: `/mcp`.

4.  Bearer token is generated and stored in `data/app-data/epson-mcp/.env`
    (gitignored).  Read it as `EPSON_MCP_AUTH_TOKEN`.

5.  Expose over Tailnet:

    ```powershell
    tailscale serve --bg --set-path=/epson http://localhost:18790
    ```

    Now accessible at `https://cyber.talpa-stargazer.ts.net/epson/mcp`.

## Wire into Codex

The epson MCP is registered globally for codex:

```bash
codex mcp add epson --url https://cyber.talpa-stargazer.ts.net/epson/mcp \
    --bearer-token-env-var EPSON_MCP_AUTH_TOKEN
```

The token is in `openclaw.json` `env.vars.EPSON_MCP_AUTH_TOKEN` so any
openclaw-launched codex session inherits it.

## Failover (Umbrel host as warm standby)

The WF-2250 is on Cyber's LAN (192.168.4.21), so Umbrel can only reach the
printer through Cyber.  A second `epson-mcp` instance on Umbrel should be
configured as a forwarder to Cyber's primary; if Cyber is unreachable, it
returns a clear "primary down" error.  This will be added in a follow-up
once SSH access from Cyber to Umbrel is available.

## Tool reference

| Tool                | Purpose                                                  |
|---------------------|----------------------------------------------------------|
| `epson_diag`        | Connectivity + capabilities summary                      |
| `epson_status`      | Re-probe printer, return summary                         |
| `epson_print_text`  | Render plain text → ESC/P-R → printer                    |
| `epson_print_file`  | Send a file from /share to the printer                   |
| `epson_print_raw`   | Send a base64-encoded ESC/P or PostScript payload        |
| `epson_ink`         | Returns "unsupported"                                    |
| `epson_list_jobs`   | List Windows spooler jobs (requires mounted helpers)    |
| `epson_cancel_job`  | Cancel a Windows spooler job by id                       |
| `epson_scan`        | Returns "not supported on WF-2250"                       |
| `epson_copy`        | Returns "not supported on WF-2250"                       |

## End-to-end test

```bash
curl -sk -H "Authorization: Bearer $EPSON_MCP_AUTH_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"epson_print_text","arguments":{"text":"hello"}}}' \
     https://cyber.talpa-stargazer.ts.net/epson/mcp
```

## Files

- `Dockerfile` — Python 3.12 Alpine, copies `server.py`, no SDK
- `docker-compose.yml` — single service, bind-mounts `win-scripts/`
- `server.py` — the MCP server (stdlib only)
- `win-scripts/{spool-helper,list-jobs,cancel-job}.ps1` — Windows-side
  helper scripts for spooler integration (optional)
- `umbrel-app.yml` — Umbrel Community App Store manifest
- `.env` — bearer token (gitignored)
- `README.md` — this file
