"""Module for IQ option websocket."""

def candles(api, message):
    if message['name'] == 'candles':
        request_id = str(message.get("request_id", ""))
        if request_id.startswith("deskubuddy-tm-"):
            pending = getattr(api, "_deskubuddy_tm_pending", None)
            lock = getattr(api, "_deskubuddy_tm_lock", None)
            if pending is not None and lock is not None:
                with lock:
                    request = pending.get(request_id)
                    if request is not None:
                        payload = message.get("msg")
                        request["data"] = (
                            payload.get("candles") if isinstance(payload, dict) else None
                        )
                        request["event"].set()
            return
        try:
            api.candles.candles_data = message["msg"]["candles"]
        except:
            pass