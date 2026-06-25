"""Amazon Bedrock access via raw boto3 (no LangChain).

Embeddings: amazon.titan-embed-text-v1 (1536-dim).
Generation: amazon.nova-lite-v1:0 (via the Converse API).

Ingestion (OCR → summary → embed) is handled by the S3-triggered Lambda in
lablumen-app/serverless/ai-service/. Only the chat-time Bedrock calls live here.

Bedrock is not enabled in the main account — calls go cross-account via STS AssumeRole,
mirroring the pattern used by the AI Lambda.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

import boto3

from .config import settings

if TYPE_CHECKING:
    from .schemas import ChatTurn


def _make_bedrock_client():
    role_arn = os.environ.get("BEDROCK_CROSS_ACCOUNT_ROLE_ARN")
    if not role_arn:
        return boto3.client("bedrock-runtime", region_name=settings.aws_region)
    sts = boto3.client("sts")
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="lablumen-report-bedrock",
    )["Credentials"]
    return boto3.client(
        "bedrock-runtime",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=settings.aws_region,
    )


_client = _make_bedrock_client()


def embed_text(text: str) -> list[float]:
    """Return the 1536-dim embedding for a piece of text."""
    resp = _client.invoke_model(
        modelId=settings.bedrock_embed_model_id,
        body=json.dumps({"inputText": text}),
        accept="application/json",
        contentType="application/json",
    )
    payload = json.loads(resp["body"].read())
    return payload["embedding"]


def generate_answer(
    system_prompt: str,
    user_prompt: str,
    history: list[ChatTurn] | None = None,
) -> str:
    """Generate a grounded answer with Nova via the Converse API."""
    messages = []
    for turn in history or []:
        messages.append({"role": turn.role, "content": [{"text": turn.content}]})
    messages.append({"role": "user", "content": [{"text": user_prompt}]})

    resp = _client.converse(
        modelId=settings.bedrock_text_model_id,
        system=[{"text": system_prompt}],
        messages=messages,
        inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
    )
    return resp["output"]["message"]["content"][0]["text"]
