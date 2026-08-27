import asyncio
import json
import logging
import os
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

rooms = {}   # room_id -> {clients, controller, state, stream}


async def websocket_handler(request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    room = "default"
    if room not in rooms:
        rooms[room] = {"clients": set(), "controller": None, "state": {}, "stream": None}

    rooms[room]["clients"].add(ws)
    logging.info(f"+ Connected | room={len(rooms[room]['clients'])} users")

    if rooms[room]["controller"] is None:
        rooms[room]["controller"] = ws
        await ws.send_str(json.dumps({"type": "control_granted"}))
    else:
        await ws.send_str(json.dumps({"type": "control_denied"}))
        if rooms[room]["state"]:
            await ws.send_str(json.dumps(rooms[room]["state"]))
        if rooms[room]["stream"]:
            await ws.send_str(json.dumps(rooms[room]["stream"]))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                t = data.get("type")

                if t == "request_control":
                    old = rooms[room]["controller"]
                    rooms[room]["controller"] = ws
                    if old and old in rooms[room]["clients"]:
                        try:
                            await old.send_str(json.dumps({"type": "control_revoked"}))
                        except Exception:
                            pass
                    await ws.send_str(json.dumps({"type": "control_granted"}))

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

                elif t == "share_start" and rooms[room]["controller"] == ws:
                    # stream_url is an already-resolved external URL (Cloudflare Tunnel)
                    payload = {
                        "type": "stream_available",
                        "stream_url": data.get("stream_url", ""),
                        "filename":   data.get("filename", ""),
                    }
                    rooms[room]["stream"] = payload
                    for c in rooms[room]["clients"]:
                        if c != ws:
                            try:
                                await c.send_str(json.dumps(payload))
                            except Exception:
                                pass
                    await ws.send_str(json.dumps({
                        "type": "share_confirmed",
                        "filename": data.get("filename", ""),
                    }))

                elif t == "share_stop":
                    rooms[room]["stream"] = None
                    for c in rooms[room]["clients"]:
                        if c != ws:
                            try:
                                await c.send_str(json.dumps({"type": "stream_unavailable"}))
                            except Exception:
                                pass

            elif msg.type == WSMsgType.ERROR:
                logging.error(f"WS error: {ws.exception()}")

    finally:
        rooms[room]["clients"].discard(ws)
        logging.info(f"- Disconnected | room={len(rooms[room]['clients'])} users")
        if rooms[room]["controller"] == ws:
            rooms[room]["controller"] = None
            rooms[room]["state"]      = {}
            rooms[room]["stream"]     = None
            if rooms[room]["clients"]:
                new_ctrl = next(iter(rooms[room]["clients"]))
                rooms[room]["controller"] = new_ctrl
                try:
                    await new_ctrl.send_str(json.dumps({"type": "control_granted"}))
                except Exception:
                    pass

    return ws


def main():
    app  = web.Application()
    app.router.add_get("/", websocket_handler)

    port = int(os.environ.get("PORT", 8765))
    logging.info("=" * 50)
    logging.info(f"  VLC Sync Server — port {port}")
    logging.info("=" * 50)

    web.run_app(app, host="0.0.0.0", port=port, access_log=None)


if __name__ == "__main__":
    main()
