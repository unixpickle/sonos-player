import html
import socket
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Literal, Sequence
from urllib.parse import urljoin

import requests

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_MX = 2
SSDP_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
TransportService = "urn:schemas-upnp-org:service:AVTransport:1"
RenderingControlService = "urn:schemas-upnp-org:service:RenderingControl:1"


@dataclass
class Device:
    """
    Represents a device/location that can play audio.
    """

    id: str  # for now, same as mac_addr
    mac_addr: str
    location: str
    friendly_name: str
    av_transport_control_url: str
    rendering_control_url: str
    volume_range: tuple[int, int]

    host_ip: str  # IP at which the device can reach this process


def ssdp_discover(timeout: float = 5.0, max_results: int = 100) -> list[dict[str, str]]:
    msg = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}",
            'MAN: "ssdp:discover"',
            f"MX: {SSDP_MX}",
            f"ST: {SSDP_ST}",
            "",
            "",
        ]
    ).encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)
    sock.sendto(msg, SSDP_ADDR)

    results: list[dict[str, str]] = []
    seen_locations = set()

    t_end = time.time() + timeout
    while time.time() < t_end and len(results) < max_results:
        try:
            data, _addr = sock.recvfrom(65535)
        except socket.timeout:
            break

        text = data.decode("utf-8", errors="ignore")
        headers: dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().upper()] = v.strip()

        loc = headers.get("LOCATION")
        if loc and loc not in seen_locations:
            seen_locations.add(loc)
            results.append(headers)

    sock.close()
    return results


def local_ip_as_seen_by_device(device_location_url: str) -> str:
    """
    Returns the local source IP your machine would use to reach the device
    at device_location_url. This is the IP the Sonos will be able to reach
    (assuming same L2/L3 reachability).
    """
    parsed = urllib.parse.urlparse(device_location_url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"Bad location URL: {device_location_url}")

    # UDP "connect" doesn't send traffic, but forces the OS to choose
    # the outgoing interface + source IP for that destination.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 1234))  # any port is fine
        local_ip = s.getsockname()[0]
        return local_ip
    finally:
        s.close()


def _xml_text(el: ET.Element | None) -> str:
    return "" if el is None or el.text is None else el.text.strip()


def _upnp_error_code(resp_text: str) -> int | None:
    try:
        root = ET.fromstring(resp_text)
    except ET.ParseError:
        return None
    # UPnP SOAP faults often include <errorCode>401</errorCode>
    el = root.find(".//{urn:schemas-upnp-org:control-1-0}errorCode")
    if el is None or not (el.text or "").strip():
        return None
    try:
        return int(el.text.strip())
    except ValueError:
        return None


class UpnpSoapError(requests.HTTPError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        upnp_error: int | None = None,
        response: requests.Response | None = None,
    ):
        super().__init__(message, response=response)
        self.status_code = status_code
        self.upnp_error = upnp_error


def get_volume_range(
    rendering_control_url: str, instance_id: int = 0
) -> tuple[int, int]:
    """
    Returns (min_volume, max_volume).
    """
    svc = RenderingControlService
    try:
        root = soap_call(
            rendering_control_url,
            svc,
            "GetVolumeRange",
            {"InstanceID": instance_id, "Channel": "Master"},
        )
        min_v = root.find(".//MinValue")
        max_v = root.find(".//MaxValue")
        if (
            min_v is not None
            and max_v is not None
            and (min_v.text or "").strip()
            and (max_v.text or "").strip()
        ):
            return int(min_v.text), int(max_v.text)
    except UpnpSoapError as e:
        if getattr(e, "upnp_error", None) == 401:
            return (0, 100)
        raise
    return (0, 100)


def load_device_info(location: str) -> Device | None:
    r = requests.get(location, timeout=3)
    r.raise_for_status()
    xml = ET.fromstring(r.text)

    ns = {"d": "urn:schemas-upnp-org:device-1-0"}
    friendly = xml.find(".//d:device/d:friendlyName", ns)
    friendly_name = _xml_text(friendly) or location
    mac_addr = _xml_text(xml.find(".//d:device/d:MACAddress", ns))

    parsed = urllib.parse.urlparse(location)
    base = f"{parsed.scheme}://{parsed.netloc}"

    av_transport_url = None
    rendering_url = None

    for service in xml.findall(".//d:serviceList/d:service", ns):
        st = _xml_text(service.find("d:serviceType", ns))
        control = _xml_text(service.find("d:controlURL", ns))
        if not st or not control:
            continue
        if st.startswith("urn:schemas-upnp-org:service:AVTransport:"):
            av_transport_url = urljoin(base, control)
        elif st.startswith("urn:schemas-upnp-org:service:RenderingControl:"):
            rendering_url = urljoin(base, control)

    if not av_transport_url or not rendering_url:
        return None

    volume_range = get_volume_range(rendering_url)

    return Device(
        id=mac_addr,
        mac_addr=mac_addr,
        location=location,
        friendly_name=friendly_name,
        av_transport_control_url=av_transport_url,
        rendering_control_url=rendering_url,
        volume_range=volume_range,
        host_ip=local_ip_as_seen_by_device(location),
    )


def soap_call(
    control_url: str, service_type: str, action: str, args: dict, timeout: float = 3.0
):
    arg_xml = "".join(
        f"<{k}>{html.escape(str(v), quote=False)}</{k}>" for k, v in args.items()
    )
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:{action} xmlns:u="{service_type}">
      {arg_xml}
    </u:{action}>
  </s:Body>
</s:Envelope>
"""
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPACTION": f'"{service_type}#{action}"',
    }
    resp = requests.post(
        control_url, data=body.encode("utf-8"), headers=headers, timeout=timeout
    )
    if not resp.ok:
        upnp_err = _upnp_error_code(resp.text)
        raise UpnpSoapError(
            f"{resp.status_code} {resp.reason} for {control_url}\n--- response body ---\n{resp.text}",
            status_code=resp.status_code,
            upnp_error=upnp_err,
            response=resp,
        )
    return ET.fromstring(resp.text)


@dataclass
class TransportState:
    state: Literal["STOPPED", "PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK"]
    uri: str
    meta: str

    @property
    def playing(self) -> bool:
        return self.state == "PLAYING"


def get_transport_state(device: Device, instance_id: int = 0) -> TransportState:
    root = soap_call(
        device.av_transport_control_url,
        TransportService,
        "GetTransportInfo",
        {"InstanceID": instance_id},
    )
    transport_state = _xml_text(root.find(".//CurrentTransportState"))

    root2 = soap_call(
        device.av_transport_control_url,
        TransportService,
        "GetMediaInfo",
        {"InstanceID": instance_id},
    )
    curi = _xml_text(root2.find(".//CurrentURI"))
    curmeta = _xml_text(root2.find(".//CurrentURIMetaData"))
    return TransportState(state=transport_state.upper(), uri=curi, meta=curmeta)


def didl_lite_meta(uri: str, title: str = "Doorbell", mime: str = "audio/mpeg") -> str:
    return f"""<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/"
  xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
  xmlns:r="urn:schemas-rinconnetworks-com:metadata-1-0/"
  xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/">
  <item id="00000000" parentID="00000000" restricted="true">
    <dc:title>{html.escape(title)}</dc:title>
    <upnp:class>object.item.audioItem.musicTrack</upnp:class>
    <res protocolInfo="http-get:*:{mime}:*">{html.escape(uri)}</res>
  </item>
</DIDL-Lite>"""


def get_volume(device: Device, instance_id: int = 0) -> int:
    if not device.rendering_control_url:
        return 0
    svc = RenderingControlService
    root = soap_call(
        device.rendering_control_url,
        svc,
        "GetVolume",
        {"InstanceID": instance_id, "Channel": "Master"},
    )
    vol = root.find(".//CurrentVolume")
    return int(vol.text) if vol is not None and vol.text else 0


def set_volume(device: Device, volume: int, instance_id: int = 0):
    if not device.rendering_control_url:
        return
    volume = max(0, min(100, int(volume)))
    svc = RenderingControlService
    soap_call(
        device.rendering_control_url,
        svc,
        "SetVolume",
        {"InstanceID": instance_id, "Channel": "Master", "DesiredVolume": volume},
    )


def set_uri(device: Device, uri: str, meta: str = "", instance_id: int = 0):
    soap_call(
        device.av_transport_control_url,
        TransportService,
        "SetAVTransportURI",
        {"InstanceID": instance_id, "CurrentURI": uri, "CurrentURIMetaData": meta},
    )


def stop(device: Device, instance_id: int = 0):
    soap_call(
        device.av_transport_control_url,
        TransportService,
        "Stop",
        {"InstanceID": instance_id},
    )


def play(device: Device, instance_id: int = 0):
    soap_call(
        device.av_transport_control_url,
        TransportService,
        "Play",
        {"InstanceID": instance_id, "Speed": 1},
    )


class SonosController:
    def __init__(self):
        self.devices: list[Device] = []
        responses = ssdp_discover(timeout=2.5, max_results=25)
        if responses:
            for h in responses:
                loc = h.get("LOCATION")
                if not loc:
                    continue
                d = load_device_info(loc)
                if d is not None:
                    self.devices.append(d)

    def play_audio(
        self,
        device_ids: Sequence[str] | None,
        url: str,
        volume: float,
        title: str = "Audio Clip",
        mime: str = "audio/mpeg",
        start_timeout_seconds: int = 5,
    ) -> float:
        devices = self.devices
        if device_ids is not None:
            devices = [x for x in self.devices if x.id in device_ids]
        return self.play_url_per_device(
            urls={d.id: url for d in devices},
            volume=volume,
            title=title,
            mime=mime,
            start_timeout_seconds=start_timeout_seconds,
        )

    def play_hosted_audio(
        self,
        device_ids: Sequence[str] | None,
        local_port: int,
        local_path: str,
        volume: float,
        title: str = "Audio Clip",
        mime: str = "audio/mpeg",
        start_timeout_seconds: int = 5,
    ) -> float:
        devices = self.devices
        if device_ids is not None:
            devices = [x for x in self.devices if x.id in device_ids]
        return self.play_url_per_device(
            urls={
                d.id: f"http://{d.host_ip}:{local_port}{local_path}" for d in devices
            },
            volume=volume,
            title=title,
            mime=mime,
            start_timeout_seconds=start_timeout_seconds,
        )

    def play_url_per_device(
        self,
        urls: dict[str, str],
        volume: float,
        title: str = "Audio Clip",
        mime: str = "audio/mpeg",
        start_timeout_seconds: int = 5,
    ) -> float:
        if volume < 0 or volume > 1:
            raise ValueError(f"volume must be in [0, 1] but got {volume=}")
        devices = [x for x in self.devices if x.id in urls.keys()]

        prev_states: list[dict[str, Any]] = []
        for d in devices:
            state = get_transport_state(d)
            vol = get_volume(d)
            prev_states.append(dict(state=state, vol=vol))

        t1 = time.time()
        try:
            for dev in devices:
                url = urls[dev.id]
                new_meta = didl_lite_meta(url, title=title, mime=mime)
                min_vol, max_vol = dev.volume_range
                stop(dev)
                set_volume(
                    dev, int(max(0, min(1, volume)) * (max_vol - min_vol) + min_vol)
                )
                set_uri(dev, url, meta=new_meta)
                play(dev)

            # Wait for all devices to start playing the URI.
            devices_switched_uri = [False] * len(devices)
            for _ in range(start_timeout_seconds):
                for i, d in enumerate(devices):
                    state = get_transport_state(d)
                    if state.uri == url:
                        devices_switched_uri[i] = True
                if all(devices_switched_uri):
                    break
                time.sleep(1)

            # Wait for the clip to finish playing on all devices.
            devices_done_playing = [False] * len(devices)
            while True:
                for i, d in enumerate(devices):
                    state = get_transport_state(d)
                    if state.uri != url or state.state not in (
                        "PLAYING",
                        "TRANSITIONING",
                    ):
                        devices_done_playing[i] = True
                if all(devices_done_playing):
                    break
                time.sleep(1)
        finally:
            # Restore original state of every device.
            for dev, prev in zip(devices, prev_states):
                prev_state = prev["state"]
                prev_vol = prev["vol"]
                try:
                    set_volume(dev, prev_vol)
                    if not prev_state.uri:
                        continue
                    set_uri(dev, prev_state.uri, prev_state.meta)
                    if prev_state.playing:
                        play(dev)
                    else:
                        stop(dev)
                except Exception as e:
                    print(f"[sonos] Restore failed: {e}", file=sys.stderr)
        return time.time() - t1
