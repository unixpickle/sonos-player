import hashlib
import io
import os
import threading
from typing import Any

from flask import Flask, abort, jsonify, request, send_file

from .speakers import Device, SonosController

AssetDir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def create_app() -> Flask:
    app = Flask(__name__)

    controller = SonosController()
    play_lock = threading.Lock()

    def _device_to_dict(d: Device) -> dict[str, Any]:
        return {
            "id": d.id,
            "name": d.friendly_name,
            "usn": d.usn,
            "location": d.location,
            "av_transport_control_url": d.av_transport_control_url,
            "rendering_control_url": d.rendering_control_url,
            "has_volume_control": bool(d.rendering_control_url),
        }

    def _coerce_float(v: Any, *, field: str) -> float:
        try:
            return float(v)
        except Exception as e:
            raise ValueError(f"'{field}' must be a number") from e

    @app.get("/")
    def record_page():
        return send_file(os.path.join(AssetDir, "index.html"))

    @app.route("/list_devices", methods=["GET"])
    def list_devices():
        devices = [_device_to_dict(d) for d in controller.devices]
        return jsonify(ok=True, count=len(devices), devices=devices)

    @app.route("/play", methods=["POST"])
    def play():
        try:
            url = request.args.get("url")
            if not url:
                raise ValueError("missing required field: url")
            volume = _coerce_float(request.args.get("volume"), field="volume")
            if (dev_ids_str := request.args.get("device_ids")) is not None:
                dev_ids = dev_ids_str.split(",")
            else:
                dev_ids = None
            title = str(
                request.args.get("title", request.args.get("title", "Audio Clip"))
            )
            mime = str(request.args.get("mime", request.args.get("mime", "audio/mpeg")))
            with play_lock:
                duration = controller.play_audio(
                    device_ids=dev_ids, url=url, volume=volume, title=title, mime=mime
                )
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(
            ok=True,
            played=True,
            url=url,
            volume=volume,
            duration=duration,
        )

    current_mime_type = None
    current_audio_bytes = None
    current_audio_hash = None

    @app.route("/play_bytes_audio", methods=["GET"])
    def play_bytes_audio():
        nonlocal current_mime_type, current_audio_bytes, current_audio_hash
        hash = request.args.get("hash")
        if hash != current_audio_hash:
            return abort(404)
        if current_audio_bytes is None:
            return abort(404)
        return send_file(
            io.BytesIO(current_audio_bytes),
            mimetype=current_mime_type,
            as_attachment=True,
            download_name="audio",
            max_age=0,
        )

    @app.route("/play_bytes", methods=["POST"])
    def play_bytes():
        nonlocal current_mime_type, current_audio_bytes, current_audio_hash

        try:
            body = request.get_data()
            mime = request.content_type
            if not body:
                raise ValueError("missing required body")

            volume = _coerce_float(request.args.get("volume"), field="volume")
            if (dev_ids_str := request.args.get("device_ids")) is not None:
                dev_ids = dev_ids_str.split(",")
            else:
                dev_ids = None
            title = str(
                request.args.get("title", request.args.get("title", "Audio Clip"))
            )
            with play_lock:
                current_mime_type = mime
                current_audio_bytes = body
                current_audio_hash = hashlib.sha1(body).hexdigest()
                duration = controller.play_hosted_audio(
                    device_ids=dev_ids,
                    local_port=get_port(),
                    local_path=f"/play_bytes_audio?hash={current_audio_hash}",
                    volume=volume,
                    title=title,
                    mime=mime,
                )
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        return jsonify(
            ok=True,
            played=True,
            volume=volume,
            duration=duration,
        )

    return app


def get_port() -> int:
    return int(os.getenv("PORT", "5000"))


# For `flask --app sonos_flask_server run ...`
app = create_app()

if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = get_port()
    app.run(host=host, port=port, threaded=True)
