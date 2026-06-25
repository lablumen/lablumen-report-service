"""Amazon Bedrock access via raw boto3 (no LangChain).

Embeddings: amazon.titan-embed-text-v1 (1536-dim).
Generation: amazon.nova-lite-v1:0 (via the Converse API).

Ingestion (OCR → summary → embed) is handled by the S3-triggered Lambda in
lablumen-app/serverless/ai-service/. Only the chat-time Bedrock calls live here.

Bedrock is not enabled in the main account — calls go cross-account via STS AssumeRole.
STS credentials are refreshed on every call (1-hour expiry) so long-running pods stay healthy.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING

import boto3

from .config import settings

if TYPE_CHECKING:
    from .schemas import ChatTurn

_ROLE_ARN = os.environ.get("BEDROCK_CROSS_ACCOUNT_ROLE_ARN")
_cached_client = None
_creds_expiry: float = 0


def _get_client():
    global _cached_client, _creds_expiry
    # Refresh 5 minutes before expiry
    if _cached_client is None or time.time() > _creds_expiry - 300:
        if not _ROLE_ARN:
            _cached_client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
            _creds_expiry = float("inf")
        else:
            sts = boto3.client("sts")
            creds = sts.assume_role(
                RoleArn=_ROLE_ARN,
                RoleSessionName="lablumen-report-bedrock",
            )["Credentials"]
            _cached_client = boto3.client(
                "bedrock-runtime",
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
                region_name=settings.aws_region,
            )
            _creds_expiry = creds["Expiration"].timestamp()
    return _cached_client


def embed_text(text: str) -> list[float]:
    resp = _get_client().invoke_model(
        modelId=settings.bedrock_embed_model_id,
        body=json.dumps({"inputText": text}),
        accept="application/json",
        contentType="application/json",
    )
    return json.loads(resp["body"].read())["embedding"]


def generate_answer(
    system_prompt: str,
    user_prompt: str,
    history: list[ChatTurn] | None = None,
) -> str:
    messages = []
    for turn in history or []:
        messages.append({"role": turn.role, "content": [{"text": turn.content}]})
    messages.append({"role": "user", "content": [{"text": user_prompt}]})

    resp = _get_client().converse(
        modelId=settings.bedrock_text_model_id,
        system=[{"text": system_prompt}],
        messages=messages,
        inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
    )
    return resp["output"]["message"]["content"][0]["text"]
