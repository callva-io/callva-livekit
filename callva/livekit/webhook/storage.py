from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core import env
from ..core.log import logger


@dataclass(frozen=True)
class Storage:
    """An S3-compatible bucket.

    One client with a configurable endpoint covers both S3 and R2; they are the same
    protocol, so there is no second code path.
    """

    bucket: str
    endpoint_url: str | None = None
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    prefix: str = ""

    @classmethod
    def from_env(cls) -> Storage | None:
        bucket = env.get("RECORDING_S3_BUCKET")
        if not bucket:
            return None
        return cls(
            bucket=bucket,
            endpoint_url=env.get("RECORDING_S3_ENDPOINT_URL"),
            region=env.get("RECORDING_S3_REGION"),
            access_key_id=env.get("RECORDING_S3_ACCESS_KEY_ID"),
            secret_access_key=env.get("RECORDING_S3_SECRET_ACCESS_KEY"),
            prefix=(env.get("RECORDING_S3_PREFIX") or "").strip("/"),
        )

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}" if self.prefix else name

    def _client(self) -> Any:
        import boto3  # imported lazily: only the [s3] extra pulls it in

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    async def put_file(self, key: str, path: Path, content_type: str) -> bool:
        def upload() -> None:
            self._client().upload_file(
                str(path), self.bucket, key, ExtraArgs={"ContentType": content_type}
            )

        return await self._run(upload, f"{path.name} -> {key}")

    async def put_json(self, key: str, document: Any) -> bool:
        body = json.dumps(document, ensure_ascii=False, default=str).encode("utf-8")

        def upload() -> None:
            self._client().put_object(
                Bucket=self.bucket, Key=key, Body=body, ContentType="application/json"
            )

        return await self._run(upload, key)

    async def _run(self, work: Any, what: str) -> bool:
        try:
            await asyncio.get_running_loop().run_in_executor(None, work)
        except ImportError:
            logger.error(
                "object storage is configured but boto3 is missing; "
                "install callva-livekit[s3]"
            )
            return False
        except Exception as exc:
            logger.error("failed to upload %s to %s: %s", what, self.bucket, exc)
            return False

        logger.debug("uploaded %s to %s", what, self.bucket)
        return True
