from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator, model_validator


class Settings(BaseSettings):
    """
    Application settings using Pydantic BaseSettings to load from environment variables.
    """
    # Kubernetes configuration
    KUBECONFIG_PATH: str
    MONITOR_INTERVAL_SECONDS: int = 60
    MONITORED_NAMESPACES: str = "*"  # Comma-separated list or "*" for all

    # Supabase configuration
    SUPABASE_URL: str
    SUPABASE_KEY: str

    # Redis configuration
    REDIS_URL: str

    # AI Service configuration (Azure OpenAI or Google Gemini)
    AI_PROVIDER: str = "azure"  # "azure" or "gemini"
    AZURE_OPENAI_API_KEY: Optional[str] = None
    AZURE_OPENAI_ENDPOINT: Optional[str] = None
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-4o"  # Model deployment name
    AZURE_OPENAI_API_VERSION: str = "2025-01-01-preview"  # API version for Azure OpenAI
    GEMINI_API_KEY: Optional[str] = None

    # SMTP configuration for alerts
    SMTP_HOST: str
    SMTP_PORT: int
    SMTP_USER: str
    SMTP_PASSWORD: str
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False
    ALERT_RECIPIENT_EMAIL: str
    ALERT_SENDER_EMAIL: Optional[str] = None

    # Allowed kubectl commands for remediation (verbs)
    ALLOWED_KUBECTL_VERBS: str = "get,describe,logs"

    # API Security
    API_KEY: str

    @field_validator('MONITORED_NAMESPACES')
    def parse_namespaces(cls, v):
        """Parse namespaces from string to list"""
        if v == "*":
            return ["*"]
        return [ns.strip() for ns in v.split(",")]

    @field_validator('ALLOWED_KUBECTL_VERBS')
    def parse_kubectl_verbs(cls, v):
        """Parse kubectl verbs from string to list"""
        if isinstance(v, str):
            return [verb.strip() for verb in v.split(",")]
        return v

    @field_validator('ALERT_SENDER_EMAIL')
    def default_sender_email(cls, v, info):
        """Set default sender email if not provided"""
        if v is None:
            # In field_validator, we can't access other fields directly
            # You might need to use model_validator to set this up properly
            return "k8s-monitor@example.com"
        return v

    @model_validator(mode='after')
    def validate_model(self):
        """Validate the entire model after all fields have been validated"""
        # Set default ALERT_SENDER_EMAIL if not provided
        if self.ALERT_SENDER_EMAIL is None:
            self.ALERT_SENDER_EMAIL = self.SMTP_USER or 'k8s-monitor@example.com'

        # Validate AI provider configuration
        if self.AI_PROVIDER.lower() == "azure":
            if not self.AZURE_OPENAI_API_KEY or not self.AZURE_OPENAI_ENDPOINT:
                raise ValueError("AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT must be provided when AI_PROVIDER is azure")
        elif self.AI_PROVIDER.lower() == "gemini":
            if not self.GEMINI_API_KEY:
                raise ValueError("GEMINI_API_KEY must be provided when AI_PROVIDER is gemini")
        else:
            raise ValueError("AI_PROVIDER must be 'azure' or 'gemini'")

        return self

    class Config:
        env_file = ".env"
        case_sensitive = True


# Create global settings instance
settings = Settings()
