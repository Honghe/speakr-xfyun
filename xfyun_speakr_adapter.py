import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from requests_toolbelt.multipart.encoder import MultipartEncoder

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("xfyun_speakr_adapter")

app = FastAPI(title="Speakr iFlytek ASR Adapter", version="1.0.0")


def load_local_env_files() -> None:
    """Load simple KEY=VALUE pairs from local env files without extra deps."""
    for candidate in (Path("env"), Path(".env")):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value:
        return value
    raise RuntimeError(
        f"Missing required environment variable {name}. "
        "Set it in the shell or add it to ./env or ./.env before starting uvicorn."
    )


load_local_env_files()

XF_APPID = require_env("XF_APPID")
XF_API_KEY = require_env("XF_API_KEY")
XF_API_SECRET = require_env("XF_API_SECRET")

# Endpoints from iFlytek Speed Transcription docs
UPLOAD_BASE = os.getenv("XF_UPLOAD_BASE", "https://upload-ost-api.xfyun.cn")
OST_BASE = os.getenv("XF_OST_BASE", "https://ost-api.xfyun.cn")

# Behavior knobs
SMALL_FILE_THRESHOLD_BYTES = int(os.getenv("SMALL_FILE_THRESHOLD_BYTES", str(30 * 1024 * 1024)))
CHUNK_SIZE_BYTES = int(os.getenv("XF_MULTIPART_CHUNK_SIZE", str(10 * 1024 * 1024)))
POLL_INTERVAL_SECONDS = float(os.getenv("XF_POLL_INTERVAL_SECONDS", "5"))
POLL_TIMEOUT_SECONDS = int(os.getenv("XF_POLL_TIMEOUT_SECONDS", "7200"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("XF_REQUEST_TIMEOUT_SECONDS", "300"))
DELETE_TEMP_FILE = os.getenv("DELETE_TEMP_FILE", "true").lower() == "true"
POSTPROC_ON = int(os.getenv("XF_POSTPROC_ON", "1"))
OUTPUT_TYPE = int(os.getenv("XF_OUTPUT_TYPE", "0"))
ENABLE_SUBTITLE = int(os.getenv("XF_ENABLE_SUBTITLE", "0"))
SMOOTHPROC = os.getenv("XF_SMOOTHPROC", "true").lower() == "true"
COLLOQPROC = os.getenv("XF_COLLOQPROC", "false").lower() == "true"
LANGUAGE_TYPE = int(os.getenv("XF_LANGUAGE_TYPE", "1"))
DHW = os.getenv("XF_DHW", "")
PD = os.getenv("XF_PD", "")


@dataclass
class SignedHeaders:
    headers: Dict[str, str]
    body: bytes


@dataclass
class AudioProbe:
    format_name: Optional[str]
    codec_name: Optional[str]
    sample_rate: Optional[int]
    channels: Optional[int]
    bits_per_sample: Optional[int]
    duration: Optional[float]
    size: Optional[int]


class XFYunAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class XFYunClient:
    def __init__(self, app_id: str, api_key: str, api_secret: str):
        self.app_id = app_id
        self.api_key = api_key
        self.api_secret = api_secret.encode("utf-8")
        self.client = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=30.0))

    def _rfc1123_date(self) -> str:
        return format_datetime(datetime.now(timezone.utc), usegmt=True)

    def _build_auth_headers(self, url: str, method: str, body: bytes, content_type: str) -> Dict[str, str]:
        parsed = urlparse(url)
        host = parsed.netloc
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        date = self._rfc1123_date()
        digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode("utf-8")
        request_line = f"{method.upper()} {path} HTTP/1.1"
        signature_origin = f"host: {host}\ndate: {date}\n{request_line}\ndigest: {digest}"
        signature = base64.b64encode(
            hmac.new(self.api_secret, signature_origin.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        authorization = (
            f'api_key="{self.api_key}", '
            f'algorithm="hmac-sha256", '
            f'headers="host date request-line digest", '
            f'signature="{signature}"'
        )
        return {
            "host": host,
            "date": date,
            "digest": digest,
            "authorization": authorization,
            "content-type": content_type,
        }

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._build_auth_headers(url, "POST", body, "application/json")
        resp = self.client.post(url, content=body, headers=headers)
        return self._parse_response(resp, url)

    def _post_multipart(self, url: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        encoder = MultipartEncoder(fields=fields)
        body = encoder.to_string()
        headers = self._build_auth_headers(url, "POST", body, encoder.content_type)
        resp = self.client.post(url, content=body, headers=headers)
        return self._parse_response(resp, url)

    def _parse_response(self, resp: httpx.Response, url: str) -> Dict[str, Any]:
        data: Optional[Dict[str, Any]] = None
        try:
            parsed = resp.json()
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            parsed = None

        if resp.is_error:
            if data is not None:
                code = data.get("code")
                message = data.get("message")
                raise XFYunAPIError(
                    f"HTTP {resp.status_code} calling {url}: code={code}, message={message}",
                    status_code=resp.status_code,
                    payload=data,
                )
            raise XFYunAPIError(
                f"HTTP {resp.status_code} calling {url}: {resp.text[:500]}",
                status_code=resp.status_code,
                payload=resp.text,
            )

        if data is None:
            try:
                data = resp.json()
            except Exception as exc:
                raise XFYunAPIError(f"Non-JSON response from {url}: {resp.text[:500]}") from exc
        code = data.get("code")
        if code != 0:
            raise XFYunAPIError(
                f"XFYun API error from {url}: code={code}, message={data.get('message')}",
                status_code=resp.status_code,
                payload=data,
            )
        return data

    def upload_small_file(self, path: Path, request_id: str) -> str:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        fields = {
            "app_id": self.app_id,
            "request_id": request_id,
            "data": (path.name, path.read_bytes(), mime),
        }
        data = self._post_multipart(f"{UPLOAD_BASE}/file/upload", fields)
        return data["data"]["url"]

    def upload_multipart_init(self, path: Path, request_id: str) -> Dict[str, Any]:
        # The multipart upload APIs use top-level params, unlike task creation/query.
        payload = {
            "request_id": request_id,
            "app_id": self.app_id,
        }
        return self._post_json(f"{UPLOAD_BASE}/file/mpupload/init", payload)

    def upload_multipart_part(
        self, request_id: str, upload_id: str, slice_id: int, chunk_bytes: bytes
    ) -> Dict[str, Any]:
        fields = {
            "request_id": request_id,
            "app_id": self.app_id,
            "upload_id": upload_id,
            "slice_id": str(slice_id),
            "data": (f"slice_{slice_id}.part", chunk_bytes, "application/octet-stream"),
        }
        return self._post_multipart(f"{UPLOAD_BASE}/file/mpupload/upload", fields)

    def upload_multipart_complete(self, request_id: str, upload_id: str) -> Dict[str, Any]:
        payload = {
            "request_id": request_id,
            "app_id": self.app_id,
            "upload_id": upload_id,
        }
        return self._post_json(f"{UPLOAD_BASE}/file/mpupload/complete", payload)

    def upload_large_file(self, path: Path, request_id: str) -> str:
        init_data = self.upload_multipart_init(path, request_id)
        upload_id = init_data["data"]["upload_id"]

        with path.open("rb") as f:
            slice_id = 1
            while True:
                chunk = f.read(CHUNK_SIZE_BYTES)
                if not chunk:
                    break
                logger.info("Uploading multipart chunk %s (%s bytes)", slice_id, len(chunk))
                self.upload_multipart_part(request_id, upload_id, slice_id, chunk)
                slice_id += 1

        complete_data = self.upload_multipart_complete(request_id, upload_id)
        return complete_data["data"]["url"]

    def create_task(
        self,
        *,
        audio_url: str,
        file_path: Path,
        request_id: str,
        language: str = "zh_cn",
        diarize: bool = False,
        speaker_num: int = 0,
    ) -> str:
        suffix = file_path.suffix.lower()
        if suffix == ".mp3":
            encoding = "lame"
        elif suffix in {".wav", ".pcm"}:
            encoding = "raw"
        else:
            # Follow docs' supported encodings; reject unknown formats early.
            raise XFYunAPIError(f"Unsupported file extension for XFYun speed transcription: {suffix}")

        payload: Dict[str, Any] = {
            "common": {"app_id": self.app_id},
            "business": {
                "request_id": request_id,
                "language": language,
                "domain": "pro_ost_ed",
                "accent": "mandarin",
                "vspp_on": 1 if diarize else 0,
                "speaker_num": speaker_num,
                "output_type": OUTPUT_TYPE,
                "postproc_on": POSTPROC_ON,
                "enable_subtitle": ENABLE_SUBTITLE,
                "smoothproc": SMOOTHPROC,
                "colloqproc": COLLOQPROC,
                "language_type": LANGUAGE_TYPE,
            },
            "data": {
                "audio_url": audio_url,
                "audio_src": "http",
                "audio_size": file_path.stat().st_size,
                "format": "audio/L16;rate=16000",
                "encoding": encoding,
            },
        }
        if DHW:
            payload["business"]["dhw"] = DHW
        if PD:
            payload["business"]["pd"] = PD
        if diarize and suffix == ".mp3":
            logger.warning("iFlytek docs say mp3 currently does not support speaker separation; disabling diarization")
            payload["business"]["vspp_on"] = 0
            payload["business"]["speaker_num"] = 0

        data = self._post_json(f"{OST_BASE}/v2/ost/pro_create", payload)
        return data["data"]["task_id"]

    def query_task(self, task_id: str) -> Dict[str, Any]:
        payload = {"common": {"app_id": self.app_id}, "business": {"task_id": task_id}}
        return self._post_json(f"{OST_BASE}/v2/ost/query", payload)


XFYUN_ERROR_HINTS = {
    10107: "Invalid XFYun encoding parameter.",
    10303: "XFYun rejected one or more request parameters.",
    10043: "XFYun could not decode the audio with the declared encoding.",
    20304: "XFYun rejected the audio as silence or as a format mismatch. This API expects 16kHz, 16-bit, mono audio; wav/pcm should use encoding=raw and mp3 should use encoding=lame.",
}

XFYUN_CLIENT_ERROR_CODES = set(XFYUN_ERROR_HINTS)


def get_xfyun_error_code(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        code = payload.get("code")
        if isinstance(code, int):
            return code
    return None


def build_xfyun_error_message(exc: XFYunAPIError) -> str:
    code = get_xfyun_error_code(exc.payload)
    if code is None:
        return str(exc)

    base = XFYUN_ERROR_HINTS.get(code)
    if not base:
        return str(exc)

    payload = exc.payload if isinstance(exc.payload, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    task_id = data.get("task_id")
    task_status = data.get("task_status")
    details: List[str] = [base, f"xfyun_code={code}"]
    if task_id:
        details.append(f"task_id={task_id}")
    if task_status:
        details.append(f"task_status={task_status}")
    return " ".join(details)


def flatten_lattice_to_text(lattice: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for seg in lattice:
        st = (((seg or {}).get("json_1best") or {}).get("st") or {})
        for rt in st.get("rt", []):
            for ws in rt.get("ws", []):
                cands = ws.get("cw", [])
                if cands:
                    w = cands[0].get("w", "")
                    if w:
                        parts.append(w)
    return "".join(parts).strip()


def parse_lattice_segments(result: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    lattice = (result or {}).get("lattice") or []
    segments: List[Dict[str, Any]] = []
    speakers_seen: List[str] = []
    full_text_parts: List[str] = []

    for item in lattice:
        st = (((item or {}).get("json_1best") or {}).get("st") or {})
        text = ""
        for rt in st.get("rt", []):
            for ws in rt.get("ws", []):
                cands = ws.get("cw", [])
                if cands:
                    text += cands[0].get("w", "")
        text = text.strip()
        speaker = item.get("spk") or st.get("rl") or "UNKNOWN_SPEAKER"
        if speaker not in speakers_seen:
            speakers_seen.append(speaker)
        start_ms = item.get("begin") or st.get("bg")
        end_ms = item.get("end") or st.get("ed")
        start = None if start_ms is None else float(start_ms) / 1000.0
        end = None if end_ms is None else float(end_ms) / 1000.0
        segments.append({
            "speaker": speaker,
            "text": text,
            "start": start,
            "end": end,
        })
        if text:
            full_text_parts.append(f"[{speaker}]: {text}")

    full_text = "\n".join(full_text_parts).strip()
    if not full_text:
        full_text = flatten_lattice_to_text(lattice)
    return full_text, segments, speakers_seen


def normalize_language(lang: Optional[str]) -> str:
    # Speakr may pass zh/en/auto. XFYun create-task wants zh_cn.
    if not lang:
        return "zh_cn"
    value = lang.lower()
    mapping = {
        "zh": "zh_cn",
        "zh-cn": "zh_cn",
        "zh_cn": "zh_cn",
        "cn": "zh_cn",
        "auto": "zh_cn",  # XFYun this API documents zh_cn here; mixed mode is controlled by language_type.
        "en": "en_us",
        "en-us": "en_us",
        "en_us": "en_us",
    }
    return mapping.get(value, lang)


def choose_speaker_num(min_speakers: Optional[int], max_speakers: Optional[int]) -> int:
    if min_speakers and max_speakers and min_speakers == max_speakers:
        return min_speakers
    return 0  # blind separation per docs


def save_upload_to_tmp(upload: UploadFile) -> Path:
    temp_dir = Path(os.getenv("TMP_DIR", "/tmp/xfyun_asr"))
    temp_dir.mkdir(parents=True, exist_ok=True)
    filename = upload.filename or f"upload-{uuid.uuid4().hex}.wav"
    target = temp_dir / f"{uuid.uuid4().hex}-{Path(filename).name}"
    with target.open("wb") as f:
        while True:
            chunk = upload.file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)
    return target


def probe_audio(path: Path) -> Optional[AudioProbe]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        logger.info("ffprobe not found; skipping audio probe for %s", path.name)
        return None

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_name,sample_rate,channels,bits_per_sample:format=duration,size,format_name",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        probe = json.loads(proc.stdout)
        stream = ((probe.get("streams") or [None])[0]) or {}
        fmt = probe.get("format") or {}
        return AudioProbe(
            format_name=fmt.get("format_name"),
            codec_name=stream.get("codec_name"),
            sample_rate=int(stream["sample_rate"]) if stream.get("sample_rate") else None,
            channels=int(stream["channels"]) if stream.get("channels") else None,
            bits_per_sample=int(stream["bits_per_sample"]) if stream.get("bits_per_sample") else None,
            duration=float(fmt["duration"]) if fmt.get("duration") else None,
            size=int(fmt["size"]) if fmt.get("size") else None,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffprobe timed out for %s", path)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        logger.warning("ffprobe failed for %s: %s", path, stderr[:500] or exc)
    except Exception:
        logger.warning("Unexpected ffprobe error for %s", path, exc_info=True)
    return None


def log_audio_probe(path: Path, probe: Optional[AudioProbe]) -> None:
    if probe is None:
        return
    logger.info(
        "Saved audio probe file=%s format=%s codec=%s sample_rate=%s channels=%s bits_per_sample=%s duration=%s size=%s",
        path.name,
        probe.format_name,
        probe.codec_name,
        probe.sample_rate,
        probe.channels,
        probe.bits_per_sample,
        probe.duration,
        probe.size,
    )


def validate_audio_for_xfyun(path: Path, probe: Optional[AudioProbe]) -> None:
    if probe is None:
        return

    suffix = path.suffix.lower()
    codec = (probe.codec_name or "").lower()

    if probe.channels not in {None, 1}:
        raise XFYunAPIError(
            f"Unsupported audio channels for XFYun: expected mono, got {probe.channels}",
            status_code=422,
            payload={"channels": probe.channels, "file": path.name},
        )

    if probe.sample_rate not in {None, 16000}:
        raise XFYunAPIError(
            f"Unsupported audio sample rate for XFYun: expected 16000 Hz, got {probe.sample_rate} Hz",
            status_code=422,
            payload={"sample_rate": probe.sample_rate, "file": path.name, "codec": probe.codec_name},
        )

    if codec == "mp3":
        if suffix != ".mp3":
            raise XFYunAPIError(
                f"Audio content/extension mismatch: file name ends with {suffix or '<none>'}, but actual codec is mp3",
                status_code=422,
                payload={"codec": probe.codec_name, "suffix": suffix, "file": path.name},
            )
        return

    if codec.startswith("pcm_"):
        if suffix not in {".wav", ".pcm"}:
            raise XFYunAPIError(
                f"Audio content/extension mismatch: file name ends with {suffix or '<none>'}, but actual codec is {probe.codec_name}",
                status_code=422,
                payload={"codec": probe.codec_name, "suffix": suffix, "file": path.name},
            )
        if probe.bits_per_sample not in {None, 16}:
            raise XFYunAPIError(
                f"Unsupported PCM bit depth for XFYun: expected 16-bit, got {probe.bits_per_sample}",
                status_code=422,
                payload={"bits_per_sample": probe.bits_per_sample, "file": path.name, "codec": probe.codec_name},
            )
        return

    raise XFYunAPIError(
        f"Unsupported audio codec for XFYun: {probe.codec_name or 'unknown'}",
        status_code=422,
        payload={
            "codec": probe.codec_name,
            "sample_rate": probe.sample_rate,
            "channels": probe.channels,
            "bits_per_sample": probe.bits_per_sample,
            "file": path.name,
        },
    )


xf_client = XFYunClient(XF_APPID, XF_API_KEY, XF_API_SECRET)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/asr")
async def asr(
    audio_file: UploadFile = File(...),
    language: Optional[str] = Query(default=None),
    diarize: bool = Query(default=False),
    enable_diarization: Optional[bool] = Query(default=None),
    min_speakers: Optional[int] = Query(default=None),
    max_speakers: Optional[int] = Query(default=None),
    initial_prompt: Optional[str] = Query(default=None),
    hotwords: Optional[str] = Query(default=None),
) -> JSONResponse:
    # Speakr sends both diarize and enable_diarization for compatibility.
    diarize_flag = enable_diarization if enable_diarization is not None else diarize

    tmp_path: Optional[Path] = None
    request_id = uuid.uuid4().hex
    try:
        tmp_path = save_upload_to_tmp(audio_file)
        probe = probe_audio(tmp_path)
        log_audio_probe(tmp_path, probe)
        validate_audio_for_xfyun(tmp_path, probe)
        file_size = tmp_path.stat().st_size
        logger.info(
            "Received ASR request file=%s size=%s language=%s diarize=%s prompt=%s hotwords=%s",
            tmp_path.name,
            file_size,
            language,
            diarize_flag,
            bool(initial_prompt),
            bool(hotwords),
        )

        if file_size < SMALL_FILE_THRESHOLD_BYTES:
            audio_url = xf_client.upload_small_file(tmp_path, request_id)
        else:
            audio_url = xf_client.upload_large_file(tmp_path, request_id)

        task_id = xf_client.create_task(
            audio_url=audio_url,
            file_path=tmp_path,
            request_id=request_id,
            language=normalize_language(language),
            diarize=diarize_flag,
            speaker_num=choose_speaker_num(min_speakers, max_speakers),
        )
        logger.info("Created XFYun task_id=%s for request_id=%s", task_id, request_id)

        started = time.time()
        last_data: Optional[Dict[str, Any]] = None
        while True:
            data = xf_client.query_task(task_id)
            last_data = data
            status = str((data.get("data") or {}).get("task_status", ""))
            if status not in {"1", "2"}:
                break
            if time.time() - started > POLL_TIMEOUT_SECONDS:
                raise XFYunAPIError(f"Timed out waiting for XFYun task {task_id}")
            time.sleep(POLL_INTERVAL_SECONDS)

        task_status = str((last_data.get("data") or {}).get("task_status", ""))
        if task_status not in {"3", "4"}:
            raise XFYunAPIError(f"Unexpected final task status {task_status}", payload=last_data)

        result = ((last_data.get("data") or {}).get("result") or {})
        text, segments, speakers = parse_lattice_segments(result)

        response = {
            "text": text,
            "language": language or "zh",
            "segments": segments,
            "speakers": speakers,
            "provider": "xfyun_speed_transcription",
            "model": "pro_ost_ed",
            "raw_response": last_data,
        }
        return JSONResponse(response)

    except XFYunAPIError as exc:
        logger.exception("XFYun API error")
        xfyun_code = get_xfyun_error_code(exc.payload)
        status_code = exc.status_code or (422 if xfyun_code in XFYUN_CLIENT_ERROR_CODES else 502)
        raise HTTPException(
            status_code=status_code,
            detail={"message": build_xfyun_error_message(exc), "payload": exc.payload},
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected adapter error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        try:
            if tmp_path and DELETE_TEMP_FILE and tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            logger.warning("Failed to delete temp file %s", tmp_path, exc_info=True)
