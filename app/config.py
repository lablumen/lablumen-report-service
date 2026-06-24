from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://lablumen:lablumen@localhost:5432/lablumen"

    aws_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""

    reports_s3_bucket: str = "lablumen-reports-local"
    presigned_url_ttl_seconds: int = 120

    bedrock_embed_model_id: str = "amazon.titan-embed-text-v1"
    # Nova Lite (v1): on-demand in us-east-1. Nova 2 Lite needs a cross-region inference profile,
    # which the org SCP (us-east-1 only) blocks. See docker-compose.yml.
    bedrock_text_model_id: str = "amazon.nova-lite-v1:0"

    # Pass as a JSON array: CORS_ORIGINS='["*"]' or CORS_ORIGINS='["http://localhost:5173"]'
    cors_origins: list[str] = ["http://localhost:5173"]


settings = Settings()
