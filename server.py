#!/usr/bin/env python3
"""
Epson WF-2250 MCP Server.

Exposes print/scan/copy/status/jobs tools over MCP-compatible JSON-RPC.  Two transports:
  - stdio:    for direct openclaw/codex integration (MCP standard)
  - HTTP:     JSON-RPC over plain HTTP, bearer-token authenticated, for Tailnet access

Backends (priority, selected by env or 'auto'):
  - raw9100:  send ESC/P-R (Epson) over TCP/9100  (default; works with WF-2250)
  - lpd:      send LPR protocol over TCP/515
  - windows:  shell out to the Windows print spooler (only when helpers are mounted)

For scanning the WF-2250 does NOT support network scanning.  The scan tool is
provided as a stub that returns a clear "not supported" message and points the
caller at the device USB / WIA path.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import secrets
import socket
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error
from typing import Any, Optional

# Optional pymupdf import for PDF conversion
try:
    import fitz  # pymupdf
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

IS_WINDOWS = sys.platform == "win32"
# On Windows prefer powershell.exe; on WSL/Linux with powershell installed use pwsh.
# If neither is available, Windows backend functions will bail out gracefully.
_PWSH_CANDIDATES = [shutil.which("powershell.exe"), shutil.which("pwsh")]
# Verify the found binary actually exists and is executable (shutil.which can return
# paths that don't work inside Docker containers or WSL2 interop)
POWERSHELL_EXE = ""
for _c in _PWSH_CANDIDATES:
    if _c and os.path.isfile(_c) and os.access(_c, os.X_OK):
        POWERSHELL_EXE = _c
        break
if not POWERSHELL_EXE:
    pass  # Windows spooler backend disabled; LOG not yet available

LOG = logging.getLogger("epson-mcp")
if not POWERSHELL_EXE:
    LOG.info("No PowerShell/pwsh found – Windows spooler backend disabled")

DEFAULT_PORT = int(os.environ.get("EPSON_MCP_PORT", "18790"))
DEFAULT_BIND = os.environ.get("EPSON_MCP_BIND", "0.0.0.0")
PRINTER_HOST = os.environ.get("EPSON_MCP_PRINTER_HOST", "192.168.4.21")
PRINTER_HOST_FALLBACK = os.environ.get("EPSON_MCP_PRINTER_HOST_FALLBACK", "192.168.4.21")
PRINTER_PORTS = {
    "raw": int(os.environ.get("EPSON_MCP_RAW_PORT", "19100")),
    "lpd": int(os.environ.get("EPSON_MCP_LPD_PORT", "515")),
}
SEND_ATTEMPTS = int(os.environ.get("EPSON_MCP_SEND_ATTEMPTS", "5"))
SEND_BACKOFF = float(os.environ.get("EPSON_MCP_SEND_BACKOFF", "0.4"))
AUTH_TOKEN = os.environ.get("EPSON_MCP_AUTH_TOKEN", "")
WINDOWS_PRINTER_NAME = os.environ.get("EPSON_MCP_WINDOWS_PRINTER", "Epson_WF2250")
SHARE_DIR = os.environ.get("EPSON_MCP_SHARE_DIR", "/share")
WINDOWS_HOST_SCRIPT_DIR = os.environ.get("EPSON_MCP_WIN_SCRIPT_DIR", "")
WINDOWS_HOST_RAW_HOST = os.environ.get("EPSON_MCP_WIN_RAW_HOST", "192.168.4.21")
WINDOWS_HOST_RAW_PORT = int(os.environ.get("EPSON_MCP_WIN_RAW_PORT", "9100"))
DEFAULT_PAPER = os.environ.get("EPSON_MCP_DEFAULT_PAPER", "A4")
DEFAULT_COPIES = int(os.environ.get("EPSON_MCP_DEFAULT_COPIES", "1"))
CONNECT_TIMEOUT = float(os.environ.get("EPSON_MCP_CONNECT_TIMEOUT", "5"))
# Epson WF-2250 has a ~52KB TCP receive buffer on port 9100.
# Sending more than this in one connection causes a timeout/reset.
# We split print jobs into per-page TCP connections, each under this limit.
TCP_MAX_PAYLOAD = int(os.environ.get("EPSON_MCP_TCP_MAX_PAYLOAD", "55000"))
# Auto-reduce DPI until each page fits under TCP_MAX_PAYLOAD bytes.
PDF_DPI_MIN = int(os.environ.get("EPSON_MCP_DPI_MIN", "72"))
READ_TIMEOUT = float(os.environ.get("EPSON_MCP_READ_TIMEOUT", "3"))
MAX_PJL_PROBE = int(os.environ.get("EPSON_MCP_PJL_PROBE", "1"))
FAILOVER_MODE = os.environ.get("EPSON_MCP_FAILOVER_MODE", "")  # "proxy" = proxy to primary
PRIMARY_URL = os.environ.get("EPSON_MCP_PRIMARY_URL", "")
PRIMARY_TOKEN = os.environ.get("EPSON_MCP_PRIMARY_TOKEN", "")
PRIMARY_TIMEOUT = float(os.environ.get("EPSON_MCP_PRIMARY_TIMEOUT", "5"))


# --- Printer state --------------------------------------------------------
@dataclass
class PrinterState:
    host: str = PRINTER_HOST
    raw_port: int = PRINTER_PORTS["raw"]
    lpd_port: int = PRINTER_PORTS["lpd"]
    pjl: bool = False
    escpr: bool = False
    last_error: str = ""
    model: str = "Epson WF-2250 (assumed)"
    note: str = ""
    capabilities: dict = field(default_factory=lambda: {
        "print": True,
        "scan_over_network": False,
        "copy_over_network": False,
        "color": True,
        "duplex": False,
        "max_paper": "A4",
    })

    def summary(self) -> dict:
        return {
            "host": self.host,
            "raw_port": self.raw_port,
            "lpd_port": self.lpd_port,
            "pjl": self.pjl,
            "escpr": self.escpr,
            "model": self.model,
            "note": self.note,
            "last_error": self.last_error,
            "capabilities": self.capabilities,
        }


STATE = PrinterState()


# --- Network probes --------------------------------------------------------
def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    for attempt in range(3):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            time.sleep(0.2 * (attempt + 1))
    return False


def _raw_send(host: str, port: int, payload: bytes, read_seconds: float = 0.0) -> bytes:
    """Send a raw payload to the printer over TCP. Respects TCP_MAX_PAYLOAD:
    if the payload exceeds the limit, it is assumed to be a multi-page ESC/P-R
    stream and will be split at page boundaries (ESC @ resets).  Each page
    segment is sent in its own TCP connection so the printer's receive buffer
    is never overwhelmed."""
    if len(payload) <= TCP_MAX_PAYLOAD:
        return _raw_send_single(host, port, payload, read_seconds)

    # Split at ESC @ (\x1b@) boundaries – each page in ESC/P-R starts with a reset.
    segments = _split_escpr_pages(payload)
    LOG.info("Payload %d bytes exceeds TCP_MAX_PAYLOAD=%d; sending %d page segment(s)",
             len(payload), TCP_MAX_PAYLOAD, len(segments))
    results = []
    for i, seg in enumerate(segments):
        LOG.info("  Sending page segment %d/%d (%d bytes)", i + 1, len(segments), len(seg))
        result = _raw_send_single(host, port, seg, read_seconds)
        results.append(result)
        # Brief pause between pages so the printer can process
        if i < len(segments) - 1:
            time.sleep(2.0)
    return b"".join(results) if results else b""


def _split_escpr_pages(data: bytes) -> list[bytes]:
    """Split ESC/P-R data at ESC @ (printer reset) boundaries.
    Each segment starts with ESC @ and ends just before the next ESC @,
    or at the end of data.  The first segment includes everything up to
    the second ESC @; the last segment includes the final ESC @ and any
    trailing data (form feed, etc.)."""
    # Find all positions of ESC @ (0x1b 0x40)
    resets = []
    for i in range(len(data) - 1):
        if data[i] == 0x1b and data[i + 1] == 0x40:
            resets.append(i)

    if not resets or len(resets) < 2:
        # No page boundaries found; return as-is
        return [data]

    # Split at each reset after the first one
    segments = []
    for i in range(len(resets)):
        start = resets[i]
        end = resets[i + 1] if i + 1 < len(resets) else len(data)
        segments.append(data[start:end])

    # Merge small trailing segments into the previous one
    merged = []
    for seg in segments:
        if merged and len(seg) < 64:
            merged[-1] += seg
        else:
            merged.append(seg)

    return merged if merged else [data]


def _raw_send_single(host: str, port: int, payload: bytes, read_seconds: float = 0.0) -> bytes:
    """Send a single payload that fits within TCP_MAX_PAYLOAD."""
    last_err: Optional[Exception] = None
    for attempt in range(max(1, SEND_ATTEMPTS)):
        try:
            with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as s:
                s.sendall(payload)
                if read_seconds <= 0:
                    return b""
                s.settimeout(READ_TIMEOUT)
                buf = b""
                deadline = time.time() + read_seconds
                while time.time() < deadline:
                    try:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    except socket.timeout:
                        continue
                return buf
        except Exception as e:
            last_err = e
            LOG.warning("raw_send attempt %d/%d to %s:%d failed: %s",
                        attempt + 1, SEND_ATTEMPTS, host, port, e)
            time.sleep(SEND_BACKOFF * (attempt + 1))
    raise last_err if last_err else RuntimeError("raw_send: no attempts made")


def probe_printer() -> None:
    """Probe the printer and fill STATE.  Runs at startup."""
    actual_host = STATE.host
    try:
        ip = socket.gethostbyname(STATE.host)
        actual_host = ip
    except Exception:
        pass

    STATE.host = actual_host
    STATE.raw_port = PRINTER_PORTS["raw"]
    STATE.lpd_port = PRINTER_PORTS["lpd"]

    raw_reachable = _tcp_probe(STATE.host, STATE.raw_port, timeout=2.0)
    lpd_reachable = _tcp_probe(STATE.host, STATE.lpd_port, timeout=2.0)

    if not raw_reachable and PRINTER_HOST_FALLBACK:
        if _tcp_probe(PRINTER_HOST_FALLBACK, STATE.raw_port, timeout=2.0):
            STATE.host = PRINTER_HOST_FALLBACK
            STATE.note = f"reached via fallback {PRINTER_HOST_FALLBACK}"
            raw_reachable = True
        elif _tcp_probe(PRINTER_HOST_FALLBACK, STATE.lpd_port, timeout=2.0):
            STATE.host = PRINTER_HOST_FALLBACK
            STATE.note = f"reached via fallback {PRINTER_HOST_FALLBACK} (LPD only)"
            lpd_reachable = True

    reachable = raw_reachable or lpd_reachable
    if raw_reachable:
        STATE.note = STATE.note or "raw+lpd"
    elif lpd_reachable:
        STATE.note = "LPD only (port 9100 closed)"
        LOG.warning("Printer port 9100 closed; using LPD backend on port 515")

    if not reachable:
        STATE.last_error = f"printer not reachable at {STATE.host} on ports {STATE.raw_port} or {STATE.lpd_port}"
        LOG.warning(STATE.last_error)
        return

    if MAX_PJL_PROBE and raw_reachable:
        try:
            pjl = b"\x1b%-12345X@PJL\r\n@PJL INFO ID\r\n@PJL ECHO ON\r\n"
            with socket.create_connection((STATE.host, STATE.raw_port), timeout=3) as s:
                s.sendall(pjl)
                s.settimeout(2)
                buf = b""
                try:
                    while True:
                        c = s.recv(1024)
                        if not c:
                            break
                        buf += c
                except socket.timeout:
                    pass
                if buf and b"@PJL" in buf.upper():
                    STATE.pjl = True
        except Exception as e:
            LOG.debug("PJL probe failed: %s", e)

    if raw_reachable:
        try:
            with socket.create_connection((STATE.host, STATE.raw_port), timeout=3) as s:
                s.sendall(b"\x1b@")
            STATE.escpr = True
        except Exception as e:
            LOG.debug("ESC/P probe failed: %s", e)


# --- Print backend --------------------------------------------------------
def _escpr_render_text(text: str, font: str = "roman", line_height: int = 30) -> bytes:
    """Render a tiny ESC/P-R document from plain text."""
    out = bytearray()
    out += b"\x1b@"             # reset
    out += b"\x1bR\x00"         # US (PC437) charset
    out += b"\x1bU\x01"         # unidirectional off
    out += b"\x1bP" + b"\x00"   # 10.5 cpi
    if font == "courier":
        out += b"\x1bX" + b"\x00" + b"\x00"
    out += b"\x1b3" + bytes([line_height])  # line spacing
    for line in text.splitlines() or [text]:
        out += line.encode("cp437", errors="replace")
        out += b"\r\n"

    out += b"\x1b@"             # reset
    out += b"\x0c"              # form feed
    return bytes(out)


def _pdf_to_escpr(pdf_path: str, dpi: int = 180) -> bytes:
    """Convert a PDF file to ESC/P-R monochrome raster data for Epson inkjet printers.

    Uses the CUPS filter pipeline (ghostscript + rastertoepson) for reliable output,
    falling back to pymupdf-based rendering if CUPS filters are unavailable.

    If the resulting ESC/P-R data for any page exceeds TCP_MAX_PAYLOAD,
    the DPI is automatically reduced through standard DPI levels (360, 300, 240,
    180, 150, 120, 90, 72) until each page fits within the limit.  Pages are rendered
    as separate ESC/P-R segments (each starting with ESC @ reset) so they can be
    sent in individual TCP connections if needed.
    """
    # Standard ESC/P-R DPI levels to try (descending quality)
    standard_dpis = [360, 300, 240, 180, 150, 120, 90, 72]
    # Find the highest standard DPI that is <= the requested dpi
    candidates = [d for d in standard_dpis if d <= dpi]
    if not candidates:
        candidates = [PDF_DPI_MIN]
    # Also try the exact requested DPI if it's not in the standard list
    if dpi not in candidates:
        candidates = [dpi] + candidates
    # Ensure minimum DPI is in the list
    if PDF_DPI_MIN not in candidates:
        candidates.append(PDF_DPI_MIN)

    for current_dpi in candidates:
        if current_dpi < PDF_DPI_MIN:
            continue
        output = _pdf_to_escpr_cups(pdf_path, current_dpi)
        if output is None:
            # CUPS filters not available, fall back to pymupdf
            output = _pdf_to_escpr_pymupdf(pdf_path, current_dpi)
        if output is None:
            continue

        # Check if each page fits within TCP_MAX_PAYLOAD
        segments = _split_escpr_pages(output)
        max_page_size = max(len(s) for s in segments) if segments else len(output)

        if max_page_size <= TCP_MAX_PAYLOAD:
            LOG.info("PDF rendered at %d dpi, max page size %d bytes (limit %d), %d pages",
                     current_dpi, max_page_size, TCP_MAX_PAYLOAD, len(segments))
            return output

        LOG.info("Page size %d bytes exceeds TCP_MAX_PAYLOAD=%d at %d dpi; trying lower dpi",
                 max_page_size, TCP_MAX_PAYLOAD, current_dpi)

    # Final attempt at minimum DPI
    LOG.warning("Rendering at minimum dpi=%d", PDF_DPI_MIN)
    output = _pdf_to_escpr_cups(pdf_path, PDF_DPI_MIN)
    if output is None:
        output = _pdf_to_escpr_pymupdf(pdf_path, PDF_DPI_MIN)
    return output or b""


def _pdf_to_escpr_cups(pdf_path: str, dpi: int) -> Optional[bytes]:
    """Convert PDF to ESC/P-R using the CUPS filter pipeline (ghostscript + rastertoepson).
    Uses temp files to avoid stdout piping issues. Returns None if CUPS filters are unavailable."""
    import subprocess
    import tempfile

    ppd_path = "/etc/cups/ppd/epson24.ppd"
    if not os.path.isfile(ppd_path):
        LOG.info("CUPS PPD not found at %s, falling back to pymupdf", ppd_path)
        return None

    gs_bin = shutil.which("gs") or ("/usr/bin/gs" if os.path.isfile("/usr/bin/gs") else None)
    if not gs_bin:
        LOG.info("ghostscript not found, falling back to pymupdf")
        return None

    rastertoepson_paths = [
        "/usr/lib/cups/filter/rastertoepson",
        "/usr/libexec/cups/filter/rastertoepson",
    ]
    rastertoepson_bin = None
    for p in rastertoepson_paths:
        if os.path.isfile(p):
            rastertoepson_bin = p
            break
    if not rastertoepson_bin:
        LOG.info("rastertoepson not found, falling back to pymupdf")
        return None

    try:
        num_pages = 1
        if HAS_PYMUPDF:
            doc = fitz.open(pdf_path)
            num_pages = len(doc)
            doc.close()

        output = bytearray()
        tmp_dir = tempfile.mkdtemp(prefix="epson_mcp_")

        try:
            for page_num in range(1, num_pages + 1):
                raster_file = os.path.join(tmp_dir, f"page_{page_num}.rast")
                escp_file = os.path.join(tmp_dir, f"page_{page_num}.escp")

                # Step 1: PDF -> CUPS Raster (ghostscript to file)
                gs_cmd = [
                    gs_bin, "-dNOPAUSE", "-dBATCH", "-dSAFER",
                    "-sDEVICE=cups",
                    f"-sOutputFile={raster_file}",
                    f"-r{dpi}",
                    f"-dFirstPage={page_num}", f"-dLastPage={page_num}",
                    "-sMediaClass=Grayscale",
                    "-sMediaType=Plain",
                    pdf_path,
                ]
                LOG.debug("Running ghostscript: %s", " ".join(gs_cmd))
                gs_proc = subprocess.run(gs_cmd, capture_output=True, timeout=30)
                if gs_proc.returncode != 0:
                    LOG.warning("ghostscript failed (page %d): %s", page_num, gs_proc.stderr.decode(errors='replace')[:200])
                    return None

                if not os.path.isfile(raster_file) or os.path.getsize(raster_file) == 0:
                    LOG.warning("ghostscript produced no raster output (page %d)", page_num)
                    return None

                # Step 2: CUPS Raster -> ESC/P-R (rastertoepson)
                with open(raster_file, "rb") as f:
                    cups_raster = f.read()

                rastertoepson_cmd = [
                    rastertoepson_bin,
                    str(page_num),    # job-id
                    "epson-mcp",      # user
                    "Print",          # title
                    "1",              # copies
                    f"resolution={dpi}dpi",  # options
                ]
                env = dict(os.environ, PPD=ppd_path)
                rastertoepson_proc = subprocess.run(
                    rastertoepson_cmd,
                    input=cups_raster,
                    capture_output=True,
                    timeout=30,
                    env=env,
                )
                if rastertoepson_proc.returncode != 0:
                    LOG.warning("rastertoepson failed (page %d): %s", page_num, rastertoepson_proc.stderr.decode(errors='replace')[:200])
                    return None

                escpr_data = rastertoepson_proc.stdout
                if not escpr_data:
                    LOG.warning("rastertoepson produced no output (page %d)", page_num)
                    return None

                output += escpr_data
                LOG.info("CUPS pipeline: page %d/%d at %d dpi -> %d bytes ESC/P-R",
                         page_num, num_pages, dpi, len(escpr_data))

        finally:
            # Clean up temp files
            import shutil as shutil_mod
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return bytes(output)

    except Exception as e:
        LOG.warning("CUPS filter pipeline failed: %s", e)
        return None


def _which(name: str) -> Optional[str]:
    """Find an executable on PATH."""
    return shutil.which(name)


def _pdf_to_escpr_pymupdf(pdf_path: str, dpi: int) -> Optional[bytes]:
    """Convert PDF to ESC/P-R using pymupdf (fallback when CUPS filters unavailable).
    Produces ESC/P-R using the ESC . raster command format."""
    if not HAS_PYMUPDF:
        return None

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        LOG.warning("Failed to open PDF %s: %s", pdf_path, e)
        return None

    output = bytearray()
    v_spacing = max(1, 360 // dpi)
    h_spacing = max(1, 360 // dpi)
    lines_per_strip = 8

    for page_num in range(len(doc)):
        page = doc[page_num]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
        w = pix.width
        h = pix.height
        samples = pix.samples

        # Page header: ESC/P setup commands (matching CUPS rastertoepson output)
        output += b"\x1b@"             # ESC @ - Reset printer
        output += b"\x1bU\x00"         # ESC U 0 - unidirectional off
        output += b"\x1b3" + bytes([v_spacing])  # ESC 3 - set line spacing
        output += b"\x0d"              # CR

        # Send raster data in 8-row strips using ESC . command
        for strip_y in range(0, h, lines_per_strip):
            num_lines = min(lines_per_strip, h - strip_y)

            # ESC . c v h nL nH data
            output += b"\x1b."                      # raster bit image command
            output += bytes([0])                     # c=0: monochrome
            output += bytes([v_spacing])             # vertical spacing
            output += bytes([h_spacing])             # horizontal spacing
            output += bytes([w % 256, w // 256])     # columns (nL nH)

            # For each column, pack 8 vertical dots into 1 byte
            for col in range(w):
                byte_val = 0
                for bit in range(lines_per_strip):
                    row = strip_y + bit
                    if row < h:
                        pixel_offset = row * w + col
                        if pixel_offset < len(samples) and samples[pixel_offset] < 128:
                            byte_val |= (1 << (7 - bit))
                output += bytes([byte_val])

            # Advance paper by strip height
            advance = num_lines * v_spacing
            output += b"\x1b(V\x01\x00"
            output += advance.to_bytes(2, "little")

        # Form feed - eject page
        output += b"\x0c"

    doc.close()
    return bytes(output)

def _post_to_windows_spooler(payload: bytes, job_name: str) -> dict:
    if not WINDOWS_HOST_SCRIPT_DIR:
        return {"ok": False, "error": "windows spooler not configured (WINDOWS_HOST_SCRIPT_DIR unset)"}
    if not POWERSHELL_EXE:
        return {"ok": False, "error": "powershell not available on this platform"}
    helper = os.path.join(WINDOWS_HOST_SCRIPT_DIR, "spool-helper.ps1")
    if not os.path.isfile(helper):
        return {"ok": False, "error": f"spool-helper not found at {helper}"}
    job_id = str(uuid.uuid4())
    in_path = os.path.join(SHARE_DIR, f"{job_id}.bin")
    out_path = os.path.join(SHARE_DIR, f"{job_id}.json")
    os.makedirs(SHARE_DIR, exist_ok=True)
    with open(in_path, "wb") as f:
        f.write(payload)
    args = [
        POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", helper,
        "-Printer", WINDOWS_PRINTER_NAME,
        "-InputFile", in_path,
        "-JobName", job_name,
        "-OutputFile", out_path,
    ]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if os.path.isfile(out_path):
            with open(out_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"ok": False, "error": proc.stderr or proc.stdout or "no output file"}
    except FileNotFoundError:
        return {"ok": False, "error": "powershell not available on this platform"}
    except Exception as e:
        return {"ok": False, "error": str(e)}



def _lpd_send(host: str, port: int, payload: bytes, job_name: str = "epson-mcp") -> dict:
    """Send a print job via RFC 1179 LPD protocol (port 515).
    
    Steps:
    1. Receive-job command: \x02 + queue + \n  -> server ack 0
    2. Control file subcommand: \x02 + size + \n -> server ack 0
    3. Control file data -> server ack 0
    4. Data file subcommand: \x03 + size + \n -> server ack 0
    5. Data file data -> server ack 0
    """
    import random
    job_id = random.randint(100, 999)
    hostname = socket.gethostname()[:32] if socket.gethostname() else "mcp"
    queue = "lp"  # standard LPD queue name for Epson
    # Data file name: dfA<NNN><hostname>
    df_name = f"dfA{job_id:03d}{hostname}"[:32]
    # Control file content
    ctrl = (
        f"H{hostname}\n"
        f"Pmcp\n"
        f"N{job_name}\n"
        f"l{df_name}\n"
        f"U{df_name}\n"
    )
    ctrl_bytes = ctrl.encode("utf-8")

    def _recv_ack(s, timeout=10):
        """Read 1-byte acknowledgment from LPD server."""
        s.settimeout(timeout)
        data = s.recv(1)
        if data == b"\x00":
            return True
        raise RuntimeError(f"LPD NACK: {data!r}")

    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    try:
        # Step 1: Receive-job command
        sock.sendall(b"\x02" + queue.encode() + b"\n")
        _recv_ack(sock)

        # Step 2: Control file subcommand
        sock.sendall(b"\x02" + str(len(ctrl_bytes)).encode() + b" " + df_name.encode() + b"\n")
        _recv_ack(sock)

        # Step 3: Control file data
        sock.sendall(ctrl_bytes)
        sock.sendall(b"\x00")  # end-of-control-file marker
        _recv_ack(sock)

        # Step 4: Data file subcommand
        sock.sendall(b"\x03" + str(len(payload)).encode() + b" " + df_name.encode() + b"\n")
        _recv_ack(sock)

        # Step 5: Data file data
        # Send in chunks for large payloads
        CHUNK = 32768
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:offset + CHUNK]
            sock.sendall(chunk)
            offset += CHUNK
            time.sleep(0.05)  # small pacing for LPD buffer

        # Signal end of data
        sock.sendall(b"\x00")
        _recv_ack(sock)

        LOG.info("LPD job %d sent successfully (%d bytes)", job_id, len(payload))
        return {"ok": True, "backend": "lpd", "bytes": len(payload), "job": job_name}
    except Exception as e:
        LOG.warning("LPD send failed: %s", e)
        raise
    finally:
        sock.close()


def print_raw(payload: bytes, job_name: str = "epson-mcp", backend: Optional[str] = None) -> dict:
    if backend is None:
        backend = "auto"
    order = ["lpd", "raw9100", "windows"] if backend == "auto" else [backend]
    last_err = ""
    for b in order:
        try:
            if b == "raw9100":
                _raw_send(STATE.host, STATE.raw_port, payload, read_seconds=0.0)
                return {"ok": True, "backend": "raw9100", "bytes": len(payload), "job": job_name}
            if b == "lpd":
                r = _lpd_send(STATE.host, STATE.lpd_port, payload, job_name=job_name)
                return r
            if b == "windows":
                r = _post_to_windows_spooler(payload, job_name)
                if r.get("ok"):
                    return {"ok": True, "backend": "windows", "result": r}
                last_err = r.get("error", "unknown")
        except Exception as e:
            last_err = f"{b}: {e}"
            continue
    return {"ok": False, "error": last_err or "no backend available"}


def print_text(text: str, copies: int = 1, paper: str = "A4",
               font: str = "roman", backend: Optional[str] = None) -> dict:
    payload = _escpr_render_text(text, font=font)
    payload = payload * max(1, copies)
    return print_raw(payload, job_name=f"text:{text[:32]}", backend=backend)


def print_file(path: str, copies: int = 1, backend: Optional[str] = None, dpi: int = 180) -> dict:
    safe = os.path.normpath(os.path.join(SHARE_DIR, os.path.basename(path)))
    if not os.path.isfile(safe):
        return {"ok": False, "error": f"file not found: {safe}"}
    with open(safe, "rb") as f:
        data = f.read()
    # Detect PDF and convert to ESC/P-R raster
    if data[:5] == b"%PDF-":
        if not HAS_PYMUPDF:
            return {"ok": False, "error": "PDF file detected but pymupdf is not installed in the container. "
                    "Rebuild the image with pymupdf support or use epson_print_text for text content."}
        try:
            LOG.info("Converting PDF %s to ESC/P-R raster (starting dpi=%d, auto-reducing if needed)", safe, dpi)
            data = _pdf_to_escpr(safe, dpi=dpi)
            LOG.info("PDF conversion complete: %d bytes total, %d pages", len(data),
                     len(_split_escpr_pages(data)))
        except Exception as e:
            return {"ok": False, "error": f"PDF conversion failed: {e}"}
    if copies > 1:
        data = data * copies
    return print_raw(data, job_name=os.path.basename(safe), backend=backend)


def scan(*args, **kwargs) -> dict:
    return {
        "ok": False,
        "supported": False,
        "error": "Network scanning is not supported on the Epson WF-2250. "
                 "Use the device's USB connection with WIA/SANE, or scan via the "
                 "device front panel to a folder if the model supports it.",
    }


def copy(*args, **kwargs) -> dict:
    return {
        "ok": False,
        "supported": False,
        "error": "Network copy is not supported on the WF-2250.  Scan to a host then print.",
    }


def list_jobs() -> dict:
    if not WINDOWS_HOST_SCRIPT_DIR:
        return {"ok": False, "error": "job control requires Windows spooler integration"}
    if not POWERSHELL_EXE:
        return {"ok": False, "error": "powershell not available on this platform"}
    helper = os.path.join(WINDOWS_HOST_SCRIPT_DIR, "list-jobs.ps1")
    if not os.path.isfile(helper):
        return {"ok": False, "error": f"helper not found: {helper}"}
    try:
        out = subprocess.check_output(
            [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", helper, "-Printer", WINDOWS_PRINTER_NAME],
            text=True, timeout=30)
        return {"ok": True, "raw": out}
    except FileNotFoundError:
        return {"ok": False, "error": "powershell not available on this platform"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cancel_job(job_id: int) -> dict:
    if not WINDOWS_HOST_SCRIPT_DIR:
        return {"ok": False, "error": "job control requires Windows spooler integration"}
    if not POWERSHELL_EXE:
        return {"ok": False, "error": "powershell not available on this platform"}
    helper = os.path.join(WINDOWS_HOST_SCRIPT_DIR, "cancel-job.ps1")
    if not os.path.isfile(helper):
        return {"ok": False, "error": f"helper not found: {helper}"}
    try:
        out = subprocess.check_output(
            [POWERSHELL_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", helper, "-Printer", WINDOWS_PRINTER_NAME,
             "-JobId", str(job_id)],
            text=True, timeout=30)
        return {"ok": True, "raw": out}
    except FileNotFoundError:
        return {"ok": False, "error": "powershell not available on this platform"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Tool list (MCP) ------------------------------------------------------
TOOLS = [
    {
        "name": "epson_diag",
        "description": "Diagnose connectivity and capabilities of the Epson printer. Returns a summary object.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "epson_print_text",
        "description": "Print a short text payload via ESC/P-R. Suitable for receipts and notes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "copies": {"type": "integer", "default": 1, "minimum": 1, "maximum": 50},
                "paper": {"type": "string", "default": "A4"},
                "font": {"type": "string", "default": "roman", "enum": ["roman", "courier"]},
                "backend": {"type": "string", "enum": ["raw9100", "lpd", "windows", "auto"], "default": "auto"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "epson_print_file",
        "description": "Print a file from /share (the bind-mount share). PDFs are auto-converted to ESC/P-R raster. "
                       "DPI is automatically reduced if a page exceeds the printer TCP buffer (~50KB). Filename only; no path traversal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string"},
                "copies": {"type": "integer", "default": 1, "minimum": 1, "maximum": 50},
                "dpi": {"type": "integer", "default": 180, "minimum": 60, "maximum": 360},
                "backend": {"type": "string", "enum": ["raw9100", "lpd", "windows", "auto"], "default": "auto"},
            },
            "required": ["filename"],
        },
    },
    {
        "name": "epson_print_raw",
        "description": "Send a base64-encoded ESC/P or PostScript payload to the printer. Returns backend used and bytes sent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data_b64": {"type": "string"},
                "job_name": {"type": "string", "default": "raw-job"},
                "backend": {"type": "string", "enum": ["raw9100", "lpd", "windows", "auto"], "default": "auto"},
            },
            "required": ["data_b64"],
        },
    },
    {
        "name": "epson_status",
        "description": "Best-effort status. Re-probes the printer.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "epson_ink",
        "description": "Ink level. Returns 'unsupported' if the model does not expose ink data over the network.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "epson_list_jobs",
        "description": "List current print jobs. Requires the Windows spooler integration (mounted helper scripts).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "epson_cancel_job",
        "description": "Cancel a print job by its Windows spooler job id.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "integer"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "epson_scan",
        "description": "Scan a page. The WF-2250 does not support network scanning; returns a clear unsupported message.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "default": "pdf", "enum": ["pdf", "png", "jpg", "tiff"]},
                "resolution_dpi": {"type": "integer", "default": 300},
                "color": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "epson_copy",
        "description": "Copy a page (scan + print). The WF-2250 does not support network copy; returns unsupported.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "copies": {"type": "integer", "default": 1, "minimum": 1, "maximum": 50},
                "duplex": {"type": "boolean", "default": False},
            },
        },
    },
]


def handle_tool_call(name: str, args: dict) -> dict:
    try:
        if name == "epson_diag":
            return {"ok": True, "printer": STATE.summary()}
        if name == "epson_print_text":
            return print_text(
                args.get("text", ""),
                copies=int(args.get("copies", 1)),
                paper=args.get("paper", "A4"),
                font=args.get("font", "roman"),
                backend=args.get("backend", "auto"),
            )
        if name == "epson_print_file":
            return print_file(
                args.get("filename", ""),
                copies=int(args.get("copies", 1)),
                backend=args.get("backend", "auto"),
                dpi=int(args.get("dpi", 180)),
            )
        if name == "epson_print_raw":
            try:
                payload = base64.b64decode(args.get("data_b64", ""))
            except Exception as e:
                return {"ok": False, "error": f"base64: {e}"}
            return print_raw(payload, job_name=args.get("job_name", "raw-job"),
                             backend=args.get("backend", "auto"))
        if name == "epson_status":
            probe_printer()
            return {"ok": True, "printer": STATE.summary()}
        if name == "epson_ink":
            return {
                "ok": False,
                "supported": False,
                "note": "Ink level is not exposed over PJL/ESC/P on the WF-2250. Use the front panel.",
            }
        if name == "epson_list_jobs":
            return list_jobs()
        if name == "epson_cancel_job":
            return cancel_job(int(args.get("job_id", 0)))
        if name == "epson_scan":
            return scan()
        if name == "epson_copy":
            return copy()
        return {"ok": False, "error": f"unknown tool: {name}"}
    except Exception as e:
        LOG.exception("tool call failed")
        return {"ok": False, "error": str(e)}


# --- MCP JSON-RPC protocol -------------------------------------------------
# A minimal MCP server: we implement the JSON-RPC 2.0 framing manually so the
# HTTP transport doesn't depend on a heavy ASGI stack.  This is the same wire
# protocol MCP uses over stdio; over HTTP we just wrap it.

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "epson-mcp", "version": "0.1.0"}


# --- Failover proxy -------------------------------------------------------
def _proxy_to_primary(payload: bytes) -> bytes:
    """Forward a JSON-RPC request to the primary (Cyber) epson-mcp instance.
    Returns the raw response bytes.  Raises on connectivity failure."""
    if not PRIMARY_URL:
        raise RuntimeError("FAILOVER_MODE=proxy but EPSON_MCP_PRIMARY_URL not set")
    headers = {"Content-Type": "application/json"}
    if PRIMARY_TOKEN:
        headers["Authorization"] = f"Bearer {PRIMARY_TOKEN}"
    req = urllib.request.Request(PRIMARY_URL, data=payload, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=PRIMARY_TIMEOUT)
        return resp.read()
    except Exception as e:
        LOG.error("failover proxy to primary %s failed: %s", PRIMARY_URL, e)
        raise


class _NotificationHandled(Exception):
    """Sentinel raised by handle_jsonrpc when the request is a JSON-RPC notification.
    Callers (handle_jsonrpc wrapper) catch this and return None so the HTTP transport
    does not emit a response body for the notification."""
    pass


def handle_jsonrpc(req: dict):
    """Dispatch a single JSON-RPC 2.0 request and return a response dict."""
    method = req.get("method", "")
    params = req.get("params", {}) or {}
    _id = req.get("id")

    def ok(result):
        return {"jsonrpc": "2.0", "id": _id, "result": result}

    def err(code, message, data=None):
        out = {"jsonrpc": "2.0", "id": _id, "error": {"code": code, "message": message}}
        if data is not None:
            out["error"]["data"] = data
        return out

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {}},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        result = handle_tool_call(name, args)
        return ok({"content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                   "isError": not result.get("ok", False)})
    if method.startswith("notifications/"):
        # JSON-RPC 2.0: notifications have no id and MUST NOT get a response.
        # Returning anything to rmcp causes "Deserialize error" deserialization failure.
        raise _NotificationHandled()
    return err(-32601, f"method not found: {method}")


# --- HTTP transport (Tailnet) --------------------------------------------
class HttpHandler(BaseHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    def _check_auth(self) -> bool:
        if not AUTH_TOKEN:
            return True
        tok = self.headers.get("authorization", "")
        if tok.lower().startswith("bearer "):
            tok = tok.split(" ", 1)[1].strip()
        else:
            tok = ""
        if not tok:
            tok = self.headers.get("x-auth-token", "")
        return bool(tok) and secrets.compare_digest(tok, AUTH_TOKEN)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-methods", "GET, POST, OPTIONS")
        self.send_header("access-control-allow-headers", "authorization, content-type, x-auth-token")
        self.end_headers()


    def do_GET(self):
        if self.path == "/healthz":
            info = {"ok": True, "service": "epson-mcp", "printer": STATE.summary()}
            if FAILOVER_MODE == "proxy":
                info["failover"] = {"mode": "proxy", "primary": PRIMARY_URL}
            return self._json(200, info)
        if self.path == "/" or self.path == "/info":
            info = {"service": "epson-mcp", "transport": "http", "tools": [t["name"] for t in TOOLS]}
            if FAILOVER_MODE == "proxy":
                info["failover"] = {"mode": "proxy", "primary": PRIMARY_URL}
            return self._json(200, info)
        if self.path == "/mcp/tools":
            if not self._check_auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            return self._json(200, {"ok": True, "tools": TOOLS})
        if self.path == "/mcp/tools/call":
            if not self._check_auth():
                return self._json(401, {"ok": False, "error": "unauthorized"})
            tool = self.headers.get("x-tool-name", "")
            if not tool:
                return self._json(400, {"ok": False, "error": "missing X-Tool-Name header"})
            try:
                body = self.rfile.read(int(self.headers.get("content-length", "0") or "0")) if False else b""
            except Exception:
                body = b""
            args = {}
            if "?" in self.path:
                from urllib.parse import parse_qs, urlparse
                qs = parse_qs(urlparse(self.path).query)
                for k, v in qs.items():
                    args[k] = v[0] if v else ""
            return self._json(200, handle_tool_call(tool, args))
        self._json(404, {"ok": False, "error": "not found", "path": self.path})

    def do_POST(self):
        # Handle file uploads to /share
        if self.path == "/upload":
            auth = self.headers.get("Authorization", "")
            if AUTH_TOKEN:
                if not auth.startswith("Bearer ") or auth[7:] != AUTH_TOKEN:
                    return self._json(401, {"ok": False, "error": "unauthorized"})
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            ct = self.headers.get("Content-Type", "")
            if ct.startswith("multipart/form-data"):
                boundary = ct.split("boundary=")[-1].strip()
                parts = body.split(b"--" + boundary.encode())
                for part in parts:
                    if b"Content-Disposition" not in part:
                        continue
                    cd_start = part.find(b"filename=")
                    if cd_start < 0:
                        continue
                    cd_start += len(b"filename=")
                    quote_char = part[cd_start:cd_start+1]
                    if quote_char in (b'"', b"'"):
                        end_quote = part.find(quote_char, cd_start + 1)
                        filename = part[cd_start + 1:end_quote].decode("utf-8")
                    else:
                        end_name = part.find(b"\r\n", cd_start)
                        filename = part[cd_start:end_name].decode("utf-8").strip()
                    header_end = part.find(b"\r\n\r\n")
                    if header_end < 0:
                        header_end = part.find(b"\n\n")
                        file_data = part[header_end + 2:]
                    else:
                        file_data = part[header_end + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
                    elif file_data.endswith(b"\n"):
                        file_data = file_data[:-1]
                    safe_name = os.path.basename(filename)
                    dest = os.path.join(SHARE_DIR, safe_name)
                    os.makedirs(SHARE_DIR, exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(file_data)
                    return self._json(200, {"ok": True, "filename": safe_name, "size": len(file_data)})
                return self._json(400, {"ok": False, "error": "no file found in upload"})
            else:
                filename = self.headers.get("X-Filename", "upload.bin")
                safe_name = os.path.basename(filename)
                dest = os.path.join(SHARE_DIR, safe_name)
                os.makedirs(SHARE_DIR, exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(body)
                return self._json(200, {"ok": True, "filename": safe_name, "size": len(body)})
        # JSON-RPC handling
        if not self._check_auth():
            return self._json(401, {"ok": False, "error": "unauthorized"})
        length = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception as e:
            return self._json(400, {"ok": False, "error": f"invalid json: {e}"})

        if self.path == "/mcp" or self.path == "/jsonrpc":
            # Failover proxy mode: forward entire request to primary
            if FAILOVER_MODE == "proxy" and PRIMARY_URL:
                try:
                    proxy_resp = _proxy_to_primary(raw)
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(proxy_resp)))
                    self.end_headers()
                    self.wfile.write(proxy_resp)
                    return
                except Exception as e:
                    # Primary unreachable: return error for each request
                    if isinstance(req, list):
                        out = []
                        for r in req:
                            if isinstance(r, dict) and "id" in r:
                                out.append({"jsonrpc": "2.0", "id": r["id"],
                                           "error": {"code": -32001,
                                                     "message": f"primary down: {e}"}})
                        self._json(200, out)
                        return
                    if isinstance(req, dict) and "id" in req:
                        return self._json(200, {"jsonrpc": "2.0", "id": req["id"],
                                                "error": {"code": -32001,
                                                          "message": f"primary down: {e}"}})
                    return self._json(502, {"ok": False, "error": f"primary down: {e}"})

            if isinstance(req, list):
                if not req:
                    return self._json(400, {"ok": False, "error": "empty batch"})
                out = []
                for r in req:
                    if not isinstance(r, dict):
                        continue
                    if "id" not in r and "method" in r:
                        # Notification in batch: must not produce a response entry
                        try:
                            handle_jsonrpc(r)
                        except _NotificationHandled:
                            pass
                        continue
                    out.append(handle_jsonrpc(r))
                return self._json(200, out)
            # Single request: if it is a notification (no id), do not respond
            if isinstance(req, dict) and "id" not in req and "method" in req:
                try:
                    handle_jsonrpc(req)
                except _NotificationHandled:
                    pass
                self.send_response(204)
                self.end_headers()
                return
            return self._json(200, handle_jsonrpc(req))

        if self.path.startswith("/mcp/tools/call"):
            if FAILOVER_MODE == "proxy" and PRIMARY_URL:
                try:
                    proxy_resp = _proxy_to_primary(raw)
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(proxy_resp)))
                    self.end_headers()
                    self.wfile.write(proxy_resp)
                    return
                except Exception as e:
                    return self._json(502, {"ok": False, "error": f"primary down: {e}"})
            if isinstance(req, dict) and "name" in req:
                return self._json(200, handle_tool_call(req["name"], req.get("arguments", {}) or {}))
            return self._json(400, {"ok": False, "error": "expected {name, arguments}"})

        return self._json(404, {"ok": False, "error": "not found", "path": self.path})


def run_http() -> None:
    server = ThreadingHTTPServer((DEFAULT_BIND, DEFAULT_PORT), HttpHandler)
    LOG.info("epson-mcp listening on http://%s:%d (auth=%s)", DEFAULT_BIND, DEFAULT_PORT,
             "on" if AUTH_TOKEN else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# --- stdio transport (MCP standard) --------------------------------------
def run_stdio() -> None:
    """Minimal stdio MCP server.  Reads JSON-RPC from stdin (one per line),
    writes responses to stdout.  Avoids the official mcp library to keep
    the image small and the transport simple."""
    LOG.info("epson-mcp listening on stdio")
    stdin = sys.stdin
    stdout = sys.stdout
    stdout.reconfigure(encoding="utf-8", line_buffering=True)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stderr.write(f"epson-mcp: bad json: {e}\n")
            continue
        resp = handle_jsonrpc(req)
        if resp.get("id") is not None or "result" in resp or "error" in resp:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "http"), default=os.environ.get("EPSON_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--log", default=os.environ.get("EPSON_MCP_LOG", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.INFO),
                        format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    LOG.info("epson-mcp starting transport=%s", args.transport)
    probe_printer()
    LOG.info("printer: %s", STATE.summary())
    if args.transport == "stdio":
        run_stdio()
    else:
        run_http()


if __name__ == "__main__":
    main()
