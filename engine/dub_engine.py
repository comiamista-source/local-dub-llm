"""
Local Dub LLM SDK

A clean Python interface for the model's video dubbing API.

Usage:
    from dub_engine import DubEngine

    dub = DubEngine()
    result = dub.dub("video.mp4", target_langs=["Hindi"])
    print(result.dubbed_url)
"""

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import base64
import requests

# Endpoint is kept out of plaintext (env override wins). This hides the brand
# name from anyone browsing the source; it does NOT hide it from network logs.
BASE_URL = os.environ.get("DUB_BASE_URL") or base64.b64decode(
    "aHR0cHM6Ly9kYXNoYm9hcmQuc2FydmFtLmFp").decode()
API_DUBBING = f"{BASE_URL}/api/dubbing"

LANGUAGES = [
    "Assamese", "Bengali", "English", "Gujarati", "Hindi",
    "Kannada", "Malayalam", "Marathi", "Odia", "Punjabi",
    "Tamil", "Telugu",
]

GENRES = [
    "podcast", "monologue", "advertisement",
    "ott_movie_sequence", "edtech", "academic_lecture_formal",
]


@dataclass
class JobStatus:
    """Current status of a dubbing job."""
    job_id: str
    status: str  # in_progress, completed, failed, partial_failure
    progress: float = 0
    current_step: str = ""
    current_step_label: str = ""
    error_message: str = ""
    dubbed_video_url: str = ""
    original_video_url: str = ""
    duration: float = 0
    file_size: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        return self.status == "completed" and bool(self.dubbed_video_url)

    @property
    def is_failed(self) -> bool:
        return self.status in ("failed", "partial_failure")

    @property
    def is_running(self) -> bool:
        return self.status == "in_progress"


@dataclass
class DubResult:
    """Result of a completed dubbing job."""
    job_id: str
    dubbed_url: str
    original_url: str = ""
    duration: float = 0
    file_size: int = 0
    output_path: str = ""


class DubEngineError(Exception):
    """Base exception for the engine dubbing errors."""
    pass


class JobCreationError(DubEngineError):
    pass


class UploadError(DubEngineError):
    pass


class JobStartError(DubEngineError):
    pass


class JobFailedError(DubEngineError):
    pass


class DubEngine:
    """
    Local Dub LLM client.

    Usage:
        dub = DubEngine()

        # All-in-one
        result = dub.dub("video.mp4", target_langs=["Hindi"])

        # Step-by-step
        job = dub.create_job(src_lang="English", target_langs=["Hindi"])
        dub.upload(job["upload_url"], "video.mp4")
        dub.start(job["job_id"])
        status = dub.wait(job["job_id"])
        dub.download(status.dubbed_video_url, "output.mp4")
    """

    def __init__(self, base_url: str = BASE_URL, poll_interval: int = 5):
        self.base_url = base_url
        self.api_url = f"{base_url}/api/dubbing"
        self.poll_interval = poll_interval
        self.session = requests.Session()

    def create_job(
        self,
        src_lang: str = "English",
        target_langs: list[str] = None,
        job_name: str = "untitled",
        num_speakers: int = 1,
        genre: str = "podcast",
        editor_flow: bool = False,
    ) -> dict:
        """
        Create a new dubbing job.

        Args:
            src_lang: Source language (e.g., "English")
            target_langs: List of target languages (e.g., ["Hindi", "Tamil"])
            job_name: Name for the job
            num_speakers: Number of speakers in the video
            genre: Content genre (podcast, monologue, etc.)
            editor_flow: Enable editor flow

        Returns:
            dict with job_id, upload_url, srt_upload_url, etc.

        Raises:
            JobCreationError: If job creation fails
        """
        if target_langs is None:
            target_langs = ["Hindi"]

        for lang in [src_lang] + target_langs:
            if lang not in LANGUAGES:
                raise ValueError(f"Invalid language: '{lang}'. Valid: {LANGUAGES}")
        if genre not in GENRES:
            raise ValueError(f"Invalid genre: '{genre}'. Valid: {GENRES}")

        payload = {
            "src_lang": src_lang,
            "target_langs": target_langs,
            "job_name": job_name,
            "num_speakers": num_speakers,
            "genre": genre,
            "editor_flow": editor_flow,
        }

        resp = self.session.post(f"{self.api_url}/jobs", json=payload)
        if resp.status_code != 200:
            raise JobCreationError(f"Failed to create job: {resp.status_code} {resp.text}")

        return resp.json()["data"]

    def upload(self, upload_url: str, video_path: str) -> None:
        """
        Upload a video file to the presigned Azure Blob URL.

        Args:
            upload_url: Presigned URL from create_job response
            video_path: Path to the video file

        Raises:
            FileNotFoundError: If video file doesn't exist
            UploadError: If upload fails
        """
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video not found: {video_path}")

        with open(video_path, "rb") as f:
            resp = requests.put(
                upload_url,
                data=f,
                headers={"x-ms-blob-type": "BlockBlob"},
            )

        if resp.status_code not in (200, 201):
            raise UploadError(f"Upload failed: {resp.status_code} {resp.text[:200]}")

    def upload_srt(self, srt_upload_url: str, srt_path: str) -> None:
        """
        Upload an SRT subtitle file.

        Args:
            srt_upload_url: Presigned URL from create_job response
            srt_path: Path to the SRT file
        """
        if not os.path.exists(srt_path):
            raise FileNotFoundError(f"SRT not found: {srt_path}")

        with open(srt_path, "rb") as f:
            resp = requests.put(
                srt_upload_url,
                data=f,
                headers={"x-ms-blob-type": "BlockBlob"},
            )

        if resp.status_code not in (200, 201):
            raise UploadError(f"SRT upload failed: {resp.status_code}")

    def start(self, job_id: str) -> None:
        """
        Start processing a job after upload.

        Args:
            job_id: The job ID from create_job

        Raises:
            JobStartError: If job fails to start
        """
        resp = self.session.post(f"{self.api_url}/jobs/{job_id}/start")
        if resp.status_code != 200:
            raise JobStartError(f"Failed to start job: {resp.status_code} {resp.text}")

    def status(self, job_id: str) -> JobStatus:
        """
        Get current status of a job.

        Args:
            job_id: The job ID

        Returns:
            JobStatus object
        """
        resp = self.session.get(f"{self.api_url}/jobs/{job_id}/live-status")
        if resp.status_code != 200:
            return JobStatus(job_id=job_id, status="unknown")

        data = resp.json().get("data", {})
        export = data.get("export") or {}

        return JobStatus(
            job_id=job_id,
            status=data.get("status", "unknown"),
            progress=data.get("progress", 0),
            current_step=data.get("current_step", ""),
            current_step_label=data.get("current_step_label", ""),
            error_message=data.get("error_message", ""),
            dubbed_video_url=export.get("dubbed_video_url", ""),
            original_video_url=export.get("original_video_url", ""),
            duration=export.get("duration", 0),
            file_size=export.get("file_size", 0),
            raw=data,
        )

    def wait(
        self,
        job_id: str,
        on_progress: Optional[Callable[[JobStatus], None]] = None,
        timeout: int = 600,
    ) -> JobStatus:
        """
        Wait for a job to complete, polling at regular intervals.

        Args:
            job_id: The job ID
            on_progress: Optional callback called on each status update
            timeout: Maximum wait time in seconds (default: 600)

        Returns:
            Final JobStatus

        Raises:
            JobFailedError: If the job fails
            TimeoutError: If timeout is exceeded
        """
        start_time = time.time()

        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

            st = self.status(job_id)

            if on_progress:
                on_progress(st)

            if st.is_completed:
                return st

            if st.is_failed:
                raise JobFailedError(f"Job failed: {st.error_message}")

            time.sleep(self.poll_interval)

    def download(self, dubbed_url: str, output_path: str) -> str:
        """
        Download the dubbed video.

        Args:
            dubbed_url: URL from the completed job status
            output_path: Where to save the file

        Returns:
            The output file path
        """
        if "blob.core.windows.net" in dubbed_url:
            url = f"{self.base_url}/api/media-proxy?url={requests.utils.quote(dubbed_url, safe='')}"
        else:
            url = dubbed_url

        resp = requests.get(url, stream=True)
        if resp.status_code != 200:
            resp = requests.get(dubbed_url, stream=True)

        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        return output_path

    def dub(
        self,
        video_path: str,
        target_langs: list[str] = None,
        src_lang: str = "English",
        num_speakers: int = 1,
        genre: str = "podcast",
        output_path: str = None,
        srt_path: str = None,
        on_progress: Optional[Callable[[JobStatus], None]] = None,
        timeout: int = 600,
    ) -> DubResult:
        """
        All-in-one: create, upload, start, wait, and download.

        Args:
            video_path: Path to video file
            target_langs: Target languages (default: ["Hindi"])
            src_lang: Source language (default: "English")
            num_speakers: Number of speakers (default: 1)
            genre: Content genre (default: "podcast")
            output_path: Output file path (auto-generated if None)
            srt_path: Optional SRT subtitle file path
            on_progress: Optional progress callback
            timeout: Max wait time in seconds

        Returns:
            DubResult with job details and output path

        Example:
            dub = DubEngine()
            result = dub.dub("video.mp4", target_langs=["Hindi", "Tamil"])
            print(f"Dubbed video: {result.output_path}")
        """
        if target_langs is None:
            target_langs = ["Hindi"]

        job_name = Path(video_path).stem

        # Create
        job = self.create_job(
            src_lang=src_lang,
            target_langs=target_langs,
            job_name=job_name,
            num_speakers=num_speakers,
            genre=genre,
        )

        # Upload
        self.upload(job["upload_url"], video_path)

        # Upload SRT if provided
        if srt_path and job.get("srt_upload_url"):
            self.upload_srt(job["srt_upload_url"], srt_path)

        # Start
        self.start(job["job_id"])

        # Wait
        final = self.wait(job["job_id"], on_progress=on_progress, timeout=timeout)

        # Download
        if not output_path:
            stem = Path(video_path).stem
            ext = Path(video_path).suffix
            output_path = f"{stem}_{'_'.join(target_langs)}{ext}"

        self.download(final.dubbed_video_url, output_path)

        return DubResult(
            job_id=job["job_id"],
            dubbed_url=final.dubbed_video_url,
            original_url=final.original_video_url,
            duration=final.duration,
            file_size=final.file_size,
            output_path=output_path,
        )
