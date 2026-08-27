import asyncio
import json
import logging
import mimetypes
import os
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Shared state ──────────────────────────────────────────────────────────────
rooms = {}
app_state = {"shared_file_path": None}


# ── WebSocket sync handler ────────────────────────────────────────────────────

async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    room = "default"
    if room not in rooms:
        rooms[room] = {"clients": set(), "controller": None, "state": {}}

    rooms[room]["clients"].add(ws)
    logging.info(f"Client connected: {request.remote}  |  Room: {len(rooms[room]['clients'])} user(s)")

    # First joiner gets control; others get the current playback state + stream info
    if rooms[room]["controller"] is None:
        rooms[room]["controller"] = ws
        await ws.send_str(json.dumps({"type": "control_granted"}))
    else:
        await ws.send_str(json.dumps({"type": "control_denied"}))
        if rooms[room]["state"]:
            await ws.send_str(json.dumps(rooms[room]["state"]))
        if app_state["shared_file_path"]:
            fname = os.path.basename(app_state["shared_file_path"])
            await ws.send_str(json.dumps({"type": "stream_available", "filename": fname}))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                t = data.get("type")

                # ── Control transfer ──────────────────────────────────────────
                if t == "request_control":
                    old = rooms[room]["controller"]
                    rooms[room]["controller"] = ws
                    if old and old in rooms[room]["clients"]:
                        try:
                            await old.send_str(json.dumps({"type": "control_revoked"}))
                        except Exception:
                            pass
                    await ws.send_str(json.dumps({"type": "control_granted"}))
                    logging.info(f"Control transferred to {request.remote}")

                # ── Playback sync broadcast ───────────────────────────────────
                elif t == "sync" and rooms[room]["controller"] == ws:
                    rooms[room]["state"] = data
                    dead = set()
                    for c in rooms[room]["clients"]:
                        if c != ws:
                            try:
                                await c.send_str(msg.data)
                            except Exception:
                                dead.add(c)
                    rooms[room]["clients"] -= dead

                # ── Start sharing a file ──────────────────────────────────────
                elif t == "share_start" and rooms[room]["controller"] == ws:
                    path = data.get("path", "")
                    if os.path.isfile(path):
                        app_state["shared_file_path"] = path
                        fname = os.path.basename(path)
                        fsize_mb = os.path.getsize(path) / (1024 * 1024)
                        logging.info(f"Sharing file: {path}  ({fsize_mb:.1f} MB)")
                        # Notify all viewers
                        for c in rooms[room]["clients"]:
                            if c != ws:
                                try:
                                    await c.send_str(json.dumps({"type": "stream_available", "filename": fname}))
                                except Exception:
                                    pass
                        await ws.send_str(json.dumps({"type": "share_confirmed", "filename": fname}))
                    else:
                        await ws.send_str(json.dumps({"type": "error", "msg": "File not found on host machine."}))

                # ── Stop sharing ──────────────────────────────────────────────
                elif t == "share_stop" and rooms[room]["controller"] == ws:
                    app_state["shared_file_path"] = None
                    for c in rooms[room]["clients"]:
                        if c != ws:
                            try:
                                await c.send_str(json.dumps({"type": "stream_unavailable"}))
                            except Exception:
                                pass
                    logging.info("File sharing stopped.")

            elif msg.type == WSMsgType.ERROR:
                logging.error(f"WebSocket error: {ws.exception()}")

    finally:
        rooms[room]["clients"].discard(ws)
        logging.info(f"Client disconnected: {request.remote}  |  Room: {len(rooms[room]['clients'])} user(s)")
        # Hand off control if the controller leaves
        if rooms[room]["controller"] == ws:
            app_state["shared_file_path"] = None
            rooms[room]["controller"] = None
            rooms[room]["state"] = {}
            if rooms[room]["clients"]:
                new_ctrl = next(iter(rooms[room]["clients"]))
                rooms[room]["controller"] = new_ctrl
                try:
                    await new_ctrl.send_str(json.dumps({"type": "control_granted"}))
                except Exception:
                    pass

    return ws


# ── HTTP video stream handler (with Range request support for seeking) ─────────

async def stream_handler(request):
    path = app_state["shared_file_path"]

    if not path or not os.path.isfile(path):
        return web.Response(status=404, text="No file is currently being shared by the host.")

    file_size = os.path.getsize(path)
    mime, _ = mimetypes.guess_type(path)
    mime = mime or "application/octet-stream"

    range_header = request.headers.get("Range")

    if range_header:
        # ── Partial content (VLC seeking) ────────────────────────────────────
        try:
            range_str = range_header.strip().replace("bytes=", "")
            parts = range_str.split("-")
            start = int(parts[0]) if parts[0] else 0
            end   = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
            end   = min(end, file_size - 1)
            length = end - start + 1

            resp = web.StreamResponse(
                status=206,
                headers={
                    "Content-Type":   mime,
                    "Content-Length": str(length),
                    "Content-Range":  f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges":  "bytes",
                    "Cache-Control":  "no-cache",
                }
            )
            await resp.prepare(request)

            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(262144, remaining))   # 256 KB chunks
                    if not chunk:
                        break
                    await resp.write(chunk)
                    remaining -= len(chunk)
            return resp

        except Exception as e:
            logging.error(f"Range request error: {e}")
            return web.Response(status=500, text=str(e))

    else:
        # ── Full file ─────────────────────────────────────────────────────────
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type":   mime,
                "Content-Length": str(file_size),
                "Accept-Ranges":  "bytes",
                "Cache-Control":  "no-cache",
            }
        )
        await resp.prepare(request)
        with open(path, "rb") as f:
            while True:
                chunk = f.read(262144)
                if not chunk:
                    break
                await resp.write(chunk)
        return resp


# ── App entry ─────────────────────────────────────────────────────────────────

def main():
    app = web.Application(client_max_size=1024 ** 3)  # allow large WS messages if needed
    app.router.add_get("/",       websocket_handler)
    app.router.add_get("/stream", stream_handler)

    port = int(os.environ.get("PORT", 8765))

    logging.info("=" * 55)
    logging.info(f"  VLC Sync Server  —  port {port}")
    logging.info(f"  WebSocket : ws://0.0.0.0:{port}/")
    logging.info(f"  Stream    : http://0.0.0.0:{port}/stream")
    logging.info("=" * 55)

    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
